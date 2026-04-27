
# 1. All imports first
from flask import Flask, jsonify, request, Response, send_from_directory, has_request_context
from flask_cors import CORS

# Prometheus metrics
try:
    from prometheus_client import (
        Counter, Histogram, Gauge, Info,
        generate_latest, CONTENT_TYPE_LATEST, CollectorRegistry, REGISTRY,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

    class _NoOp:
        """Stub that swallows any method call so metric code is a no-op."""
        def __getattr__(self, _):
            return lambda *a, **kw: self
        def __call__(self, *a, **kw):
            return self

    Counter = Histogram = Gauge = Info = lambda *a, **kw: _NoOp()
    generate_latest = lambda *a, **kw: b''
    CONTENT_TYPE_LATEST = 'text/plain'
    REGISTRY = None
    print("\u26a0\ufe0f  prometheus-client not installed. Metrics disabled. "
          "Run: pip install prometheus-client")

import shutil
import subprocess
import json
import re
import hashlib
from pathlib import Path
import threading
import queue
import time
import argparse
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
import os
import signal
import sys
import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.style import Style
import questionary
from simulation_service_tool.services.command_runner import run_cli_command
from simulation_service_tool.services.k8s_native import (
    K8S_AVAILABLE, K8sNativeMonitor, REQUEST_TIMEOUT,
    initialize_native_k8s_clients, native_k8s_client_enabled,
    v1, batch_v1, policy_v1, K8S_CLIENT_DISABLED_REASON,
)

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
except ImportError:
    client = None
    config = None
    ApiException = Exception

# 3. Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Suppress noisy Werkzeug access logs for high-frequency dashboard polling endpoints
class _PollFilter(logging.Filter):
    _SKIP = re.compile(r'GET /api/simulation/(activity|summary|agent-states|live-logs)')
    def filter(self, record):
        return not self._SKIP.search(record.getMessage())
logging.getLogger('werkzeug').addFilter(_PollFilter())

# 4. CREATE FLASK APP BEFORE ROUTES
app = Flask(__name__)
CORS(app, origins=[
    'http://localhost:5173', 'http://localhost:3001', 'http://localhost:3002',
    'http://localhost:3003', 'http://localhost:4173', 'http://localhost:8080',
    'http://localhost:5002', 'http://localhost:5174', 'http://localhost:5175',
], supports_credentials=True)

_DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), 'dashboard')

# --- Prometheus Metrics ---
SIM_BUILD_INFO = Info('simulation_service', 'Simulation service build info')
SIM_BUILD_INFO.info({'version': '1.0.0', 'service': 'simulation-service'})

SIM_REQUESTS_TOTAL = Counter(
    'simulation_http_requests_total',
    'Total HTTP requests to the simulation service',
    ['method', 'endpoint', 'status'],
)
SIM_REQUEST_DURATION = Histogram(
    'simulation_http_request_duration_seconds',
    'HTTP request latency in seconds',
    ['method', 'endpoint'],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)
AGENT_ORCHESTRATION_TOTAL = Counter(
    'agent_orchestration_total',
    'Total simulation agent orchestration events',
    ['action', 'result'],
)
AGENT_TEST_DURATION = Histogram(
    'agent_test_duration_seconds',
    'Duration of agent test runs in seconds',
    ['preset', 'persona'],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800],
)
AGENT_PODS_ACTIVE = Gauge(
    'agent_pods_active',
    'Number of currently active agent pods',
)
AGENT_PODS_SUCCEEDED = Gauge(
    'agent_pods_succeeded',
    'Number of succeeded agent pods',
)
AGENT_PODS_FAILED = Gauge(
    'agent_pods_failed',
    'Number of failed agent pods',
)
AGENT_PODS_PENDING = Gauge(
    'agent_pods_pending',
    'Number of pending agent pods',
)
DIAGNOSTICS_TRIGGERED_TOTAL = Counter(
    'diagnostics_triggered_total',
    'Total diagnostics invocations',
    ['diagnostic_type'],
)
CLEANUP_OPERATIONS_TOTAL = Counter(
    'cleanup_operations_total',
    'Total cleanup operations performed',
    ['resource_type', 'result'],
)
HELM_OPERATIONS_TOTAL = Counter(
    'helm_operations_total',
    'Total Helm operations',
    ['operation', 'status'],
)
KUEUE_PENDING_WORKLOADS = Gauge(
    'kueue_pending_workloads',
    'Number of workloads waiting in Kueue queue',
)
KUEUE_ADMITTED_WORKLOADS = Gauge(
    'kueue_admitted_workloads',
    'Number of workloads admitted by Kueue',
)
KUEUE_ACTIVE = Gauge(
    'kueue_active',
    'Whether Kueue is installed and active (1=yes, 0=no)',
)
SIMULATION_ACTIVE_TEST = Info(
    'simulation_active_test',
    'Currently active simulation test configuration',
)
# Initialise to empty so the metric is always present
SIMULATION_ACTIVE_TEST.info({'target_url': '', 'probe_mode': '', 'test_name': '', 'completions': '', 'parallelism': ''})
_EMPTY_ACTIVE_TEST = {'target_url': '', 'probe_mode': '', 'test_name': '', 'completions': '', 'parallelism': ''}
_ACTIVE_TEST_STATE_PATH = Path(__file__).with_name('.simulation_active_test_state.json')


def _load_last_active_test() -> dict:
    try:
        payload = json.loads(_ACTIVE_TEST_STATE_PATH.read_text())
    except Exception:
        return dict(_EMPTY_ACTIVE_TEST)

    return {
        'target_url': str(payload.get('target_url') or ''),
        'probe_mode': str(payload.get('probe_mode') or ''),
        'test_name': str(payload.get('test_name') or ''),
        'completions': str(payload.get('completions') or ''),
        'parallelism': str(payload.get('parallelism') or ''),
    }


def _persist_last_active_test(payload: dict) -> None:
    try:
        _ACTIVE_TEST_STATE_PATH.write_text(json.dumps(payload))
    except Exception:
        logger.debug('Failed to persist last active test state', exc_info=True)


_LAST_ACTIVE_TEST = _load_last_active_test()

# ── Application constants ──────────────────────────────────────────────────────

SIMULATION_MODES = ['basic', 'transactional']

PRESETS = {
    'tiny': {
        'completions': 5,
        'parallelism': 2,
        'persona': 'impatient',
        'workers': 1,
        'mode': 'basic',
    },
    'small': {
        'completions': 10,
        'parallelism': 5,
        'persona': 'impatient',
        'workers': 1,
        'mode': 'basic',
    },
    'medium': {
        'completions': 50,
        'parallelism': 10,
        'persona': 'strategic',
        'workers': 1,
        'mode': 'basic',
    },
    'large': {
        'completions': 100,
        'parallelism': 20,
        'persona': 'browser',
        'workers': 1,
        'mode': 'basic',
    },
    'xlarge': {
        'completions': 500,
        'parallelism': 50,
        'persona': 'browser',
        'workers': 1,
        'mode': 'basic',
    },
}

@app.before_request
def _track_request_start():
    request._prom_start = time.time()

@app.after_request
def _track_request_metrics(response):
    if hasattr(request, '_prom_start'):
        duration = time.time() - request._prom_start
        endpoint = request.path
        SIM_REQUESTS_TOTAL.labels(
            method=request.method,
            endpoint=endpoint,
            status=response.status_code,
        ).inc()
        SIM_REQUEST_DURATION.labels(
            method=request.method,
            endpoint=endpoint,
        ).observe(duration)
    return response

@app.route('/metrics')
def prometheus_metrics():
    """Prometheus metrics endpoint."""
    return Response(generate_latest(REGISTRY), mimetype=CONTENT_TYPE_LATEST)

# --- API Key Authentication ---
SIMULATION_API_KEY = os.environ.get('SIMULATION_API_KEY', 'dev-key-change-in-production')

def require_api_key(f):
    """Decorator to require API key for mutating simulation endpoints."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization')
        if not auth or auth != f"Bearer {SIMULATION_API_KEY}":
            return jsonify({'error': 'Unauthorized. Provide Authorization: Bearer <api-key>'}), 401
        return f(*args, **kwargs)
    return decorated

# --- Input Validation ---
def is_valid_release_name(name: str) -> bool:
    """Validate release name against Kubernetes DNS-1123 label rules."""
    return bool(name and len(name) <= 53 and re.match(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$', name))

def is_valid_persona(persona: str) -> bool:
    """Validate persona name contains only safe characters."""
    return bool(persona and len(persona) <= 30 and re.match(r'^[a-zA-Z0-9_-]+$', persona))


# K8s client init, host validation, native_k8s_client_enabled,
# v1/batch_v1/policy_v1 singletons are all provided by
# simulation_service_tool.services.k8s_native (imported above).


HELM_RELEASE_ANNOTATION = "meta.helm.sh/release-name"


def _list_release_owned_resource_names(resource: str, release_name: str) -> List[str]:
    names = set()

    labeled_result = run_cli_command(["kubectl", "get", resource, "-l", f"release={release_name}", "-o", "name"])
    if labeled_result.returncode == 0 and labeled_result.stdout.strip():
        for line in labeled_result.stdout.strip().split('\n'):
            line = line.strip()
            if line:
                names.add(line.split('/', 1)[-1])

    owned_result = run_cli_command(["kubectl", "get", resource, "-o", "json"])
    if owned_result.returncode == 0 and owned_result.stdout.strip():
        try:
            payload = json.loads(owned_result.stdout)
        except json.JSONDecodeError:
            payload = {}
        for item in payload.get("items", []):
            metadata = item.get("metadata", {})
            annotations = metadata.get("annotations", {}) or {}
            if annotations.get(HELM_RELEASE_ANNOTATION) == release_name:
                name = metadata.get("name")
                if name:
                    names.add(name)

    return sorted(names)


def release_exists_or_has_resources(release_name: str) -> bool:
    if not is_valid_release_name(release_name):
        return False

    helm_result = run_cli_command(["helm", "status", release_name], namespace='default')
    if helm_result.returncode == 0:
        return True

    return any(
        _list_release_owned_resource_names(resource, release_name)
        for resource in ("pods", "pvc", "pdb", "jobs")
    )


def _release_manifest_mentions_playwright(release_name: str, namespace: str = "default") -> bool:
    if not is_valid_release_name(release_name):
        return False

    manifest_result = run_cli_command(["helm", "get", "manifest", release_name], namespace=namespace)
    if manifest_result.returncode != 0:
        return False

    manifest_text = (manifest_result.stdout or "").lower()
    return any(
        marker in manifest_text
        for marker in (
            'app: playwright-agent',
            'app.kubernetes.io/name: playwright-agent',
            'name: playwright-agent',
        )
    )


def is_playwright_release(release_name: str, namespace: str = "default") -> bool:
    if not is_valid_release_name(release_name):
        return False

    if any(
        _list_release_owned_resource_names(resource, release_name)
        for resource in ("pods", "pvc", "pdb", "jobs")
    ):
        return True

    return _release_manifest_mentions_playwright(release_name, namespace)


def list_playwright_releases(namespace: str = "default") -> List[Dict[str, Any]]:
    result = run_cli_command(["helm", "list", "-o", "json"], namespace=namespace)
    if result.returncode != 0 or not result.stdout.strip():
        return []

    try:
        releases = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    return [
        release for release in releases
        if is_playwright_release(release.get("name", ""), namespace)
    ]

# 5. Define classes
class K8sSimulationMonitor:
    # ... your monitor code ...

    @staticmethod
    def preflight_check():
        """Check for conflicts before starting a test"""
        conflicts = []

        # Check for existing PVC
        pvc_result = run_cli_command(
            ["kubectl", "get", "pvc", "playwright-cache", "-o", "name"]
        )
        if pvc_result.returncode == 0 and pvc_result.stdout.strip():
            conflicts.append({
                'type': 'pvc',
                'name': 'playwright-cache',
                'fix': 'kubectl delete pvc playwright-cache'
            })

        # Check for existing PDB
        pdb_result = run_cli_command(
            ["kubectl", "get", "pdb", "playwright-agent-pdb", "-o", "name"]
        )
        if pdb_result.returncode == 0 and pdb_result.stdout.strip():
            conflicts.append({
                'type': 'pdb',
                'name': 'playwright-agent-pdb',
                'fix': 'kubectl delete pdb playwright-agent-pdb',
                'note': 'Legacy shared PDB from older chart versions'
            })

        # Check for existing helm releases
        helm_result = run_cli_command(["helm", "list", "--short"])
        if helm_result.returncode == 0 and helm_result.stdout and helm_result.stdout.strip():
            releases = [r for r in helm_result.stdout.strip().split('\n') if r]
            if releases:
                conflicts.append({
                    'type': 'helm_releases',
                    'releases': releases,
                    'fix': 'helm uninstall <release-name>'
                })

        return {
            'has_conflicts': len(conflicts) > 0,
            'conflicts': conflicts
        }

# 6. Initialize handlers AFTER classes are defined (real instances assigned later)

# 7. Routes are registered below after all classes are defined.

# --- Cluster Cleanup Module ---
class ClusterCleanup:
    """Handles cleanup of stuck resources and orphaned artifacts"""
    def __init__(self, namespace="default"):
        self.namespace = namespace
        self.v1 = v1
        self.batch_v1 = batch_v1
        self.policy_v1 = policy_v1

    def cleanup_all(self, dry_run=False):
        results = {
            'helm_releases': self.cleanup_helm_releases(dry_run),
            'stuck_resources': self.cleanup_stuck_resources(dry_run),
            'completed_pods': self.cleanup_completed_pods(dry_run),
            'orphaned_pvcs': self.cleanup_orphaned_pvcs(dry_run),
            'conflicting_pdbs': self.cleanup_pdbs(dry_run)
        }
        return results

    def cleanup_helm_releases(self, dry_run=False):
        result = run_cli_command(["helm", "list", "--short"], namespace=self.namespace)
        cleaned = []
        if result.stdout:
            for release in result.stdout.strip().split('\n'):
                if release and ('test' in release.lower() or 'sim' in release.lower()):
                    if not is_valid_release_name(release):
                        logger.warning(f"Skipping invalid release name: {release}")
                        continue
                    if not dry_run:
                        run_cli_command(["helm", "uninstall", release, "--ignore-not-found"], namespace=self.namespace)
                    cleaned.append(release)
        return {'count': len(cleaned), 'releases': cleaned}

    def cleanup_stuck_resources(self, dry_run=False):
        resources = ['pod', 'service', 'deployment', 'replicaset', 
                     'statefulset', 'job', 'secret', 'configmap']
        cleaned = []
        for resource in resources:
            cmd = ["kubectl", "delete", resource, "-l", "app=playwright-agent", "--ignore-not-found"]
            if dry_run:
                cmd.append("--dry-run=client")
            result = run_cli_command(cmd, namespace=self.namespace)
            if result.returncode == 0 and result.stdout:
                cleaned.append(resource)
        return {'count': len(cleaned), 'resources': cleaned}

    def cleanup_completed_pods(self, dry_run=False):
        if self.v1 is None:
            cmd = ["kubectl", "get", "pods", "--field-selector=status.phase=Succeeded", "--no-headers"]
            result = run_cli_command(cmd, namespace=self.namespace)
            pod_names = [line.split()[0] for line in result.stdout.strip().split('\n') if line.strip()] if result.stdout.strip() else []
            if not dry_run:
                for pod_name in pod_names:
                    run_cli_command(["kubectl", "delete", "pod", pod_name, "--ignore-not-found"], namespace=self.namespace)
            return {'count': len(pod_names), 'pods': pod_names}
        try:
            pods = self.v1.list_namespaced_pod(
                namespace=self.namespace,
                field_selector="status.phase=Succeeded"
            )
            cleaned = []
            for pod in pods.items:
                if not dry_run:
                    self.v1.delete_namespaced_pod(
                        name=pod.metadata.name,
                        namespace=self.namespace
                    )
                cleaned.append(pod.metadata.name)
            return {'count': len(cleaned), 'pods': cleaned}
        except Exception:
            cmd = ["kubectl", "get", "pods", "--field-selector=status.phase=Succeeded", "--no-headers"]
            result = run_cli_command(cmd, namespace=self.namespace)
            pod_names = [line.split()[0] for line in result.stdout.strip().split('\n') if line.strip()] if result.stdout.strip() else []
            if not dry_run:
                for pod_name in pod_names:
                    run_cli_command(["kubectl", "delete", "pod", pod_name, "--ignore-not-found"], namespace=self.namespace)
            return {'count': len(pod_names), 'pods': pod_names}

    def cleanup_orphaned_pvcs(self, dry_run=False):
        if self.v1 is None:
            cmd = ["kubectl", "get", "pvc", "-l", "app=playwright-agent", "--no-headers"]
            result = run_cli_command(cmd, namespace=self.namespace)
            pvc_names = [line.split()[0] for line in result.stdout.strip().split('\n') if line.strip()] if result.stdout.strip() else []
            filtered = [name for name in pvc_names if 'playwright' in name or 'test' in name]
            if not dry_run:
                for pvc_name in filtered:
                    run_cli_command(["kubectl", "delete", "pvc", pvc_name, "--ignore-not-found"], namespace=self.namespace)
            return {'count': len(filtered), 'pvcs': filtered}
        try:
            pvcs = self.v1.list_namespaced_persistent_volume_claim(
                namespace=self.namespace,
                label_selector="app=playwright-agent"
            )
            cleaned = []
            for pvc in pvcs.items:
                if pvc.status.phase == "Bound":
                    # Check if bound pod still exists
                    pod_name = pvc.spec.volume_name
                    # Simple check - delete if it's a test PVC
                    if 'playwright' in pvc.metadata.name or 'test' in pvc.metadata.name:
                        if not dry_run:
                            self.v1.delete_namespaced_persistent_volume_claim(
                                name=pvc.metadata.name,
                                namespace=self.namespace
                            )
                        cleaned.append(pvc.metadata.name)
            return {'count': len(cleaned), 'pvcs': cleaned}
        except Exception:
            cmd = ["kubectl", "get", "pvc", "-l", "app=playwright-agent", "--no-headers"]
            result = run_cli_command(cmd, namespace=self.namespace)
            pvc_names = [line.split()[0] for line in result.stdout.strip().split('\n') if line.strip()] if result.stdout.strip() else []
            filtered = [name for name in pvc_names if 'playwright' in name or 'test' in name]
            if not dry_run:
                for pvc_name in filtered:
                    run_cli_command(["kubectl", "delete", "pvc", pvc_name, "--ignore-not-found"], namespace=self.namespace)
            return {'count': len(filtered), 'pvcs': filtered}

    def cleanup_pdbs(self, dry_run=False):
        if self.policy_v1 is None:
            cleaned = []
            result = run_cli_command(["kubectl", "get", "pdb", "-o", "name"], namespace=self.namespace)
            pdb_names = [line.replace('poddisruptionbudget.policy/', '').strip() for line in result.stdout.strip().split('\n') if line.strip()] if result.stdout.strip() else []
            for pdb_name in pdb_names:
                if 'playwright' in pdb_name or 'test' in pdb_name:
                    if not dry_run:
                        run_cli_command(["kubectl", "delete", "pdb", pdb_name, "--ignore-not-found"], namespace=self.namespace)
                    cleaned.append(pdb_name)
            return {'count': len(cleaned), 'pdbs': cleaned}
        try:
            pdbs = self.policy_v1.list_namespaced_pod_disruption_budget(
                namespace=self.namespace,
                label_selector="app=playwright-agent"
            )
            cleaned = []
            for pdb in pdbs.items:
                if not dry_run:
                    self.policy_v1.delete_namespaced_pod_disruption_budget(
                        name=pdb.metadata.name,
                        namespace=self.namespace
                    )
                cleaned.append(pdb.metadata.name)
            # Also check for specifically named PDB
            specific_pdbs = ['playwright-agent-pdb', 'playwright-cache-pdb']
            for pdb_name in specific_pdbs:
                try:
                    self.policy_v1.read_namespaced_pod_disruption_budget(
                        name=pdb_name,
                        namespace=self.namespace
                    )
                    if not dry_run:
                        self.policy_v1.delete_namespaced_pod_disruption_budget(
                            name=pdb_name,
                            namespace=self.namespace
                        )
                    cleaned.append(pdb_name)
                except ApiException:
                    pass  # Doesn't exist
            return {'count': len(cleaned), 'pdbs': cleaned}
        except Exception:
            cleaned = []
            result = run_cli_command(["kubectl", "get", "pdb", "-o", "name"], namespace=self.namespace)
            pdb_names = [line.replace('poddisruptionbudget.policy/', '').strip() for line in result.stdout.strip().split('\n') if line.strip()] if result.stdout.strip() else []
            for pdb_name in pdb_names:
                if 'playwright' in pdb_name or 'test' in pdb_name:
                    if not dry_run:
                        run_cli_command(["kubectl", "delete", "pdb", pdb_name, "--ignore-not-found"], namespace=self.namespace)
                    cleaned.append(pdb_name)
            return {'count': len(cleaned), 'pdbs': cleaned}

    def cleanup_specific_release(self, release_name, dry_run=False):
        if not is_valid_release_name(release_name):
            return {'error': f'Invalid release name: {release_name}'}

        results = {
            'release': release_name,
            'helm': None,
            'pods': [],
            'pvcs': [],
            'pdbs': [],
            'jobs': []
        }
        if not release_exists_or_has_resources(release_name):
            results['helm'] = 'not found'
            results['warning'] = f"No Helm release or owned resources found for '{release_name}'."
            return results
        # Uninstall Helm release
        if not dry_run:
            run_cli_command(["helm", "uninstall", release_name, "--ignore-not-found"], namespace=self.namespace)
        results['helm'] = 'uninstalled'
        for resource, key in (("pods", 'pods'), ("pvc", 'pvcs'), ("pdb", 'pdbs'), ("jobs", 'jobs')):
            names = _list_release_owned_resource_names(resource, release_name)
            if not dry_run:
                for name in names:
                    run_cli_command(["kubectl", "delete", resource, name, "--ignore-not-found"], namespace=self.namespace)
            results[key].extend(names)

        return results

    def reset_cluster_state(self, dry_run=False):
        print("\n🧹 Resetting cluster state...")
        results = self.cleanup_all(dry_run)
        if not dry_run:
            time.sleep(3)
        verification = self.verify_clean_state()
        results['verification'] = verification
        return results

    def verify_clean_state(self):
        checks = {}
        checks['helm_test_releases'] = len(list_playwright_releases(self.namespace))
        if self.v1 is None:
            result = run_cli_command(["kubectl", "get", "pods", "-l", "app=playwright-agent", "-o", "name"], namespace=self.namespace)
            pod_lines = [l for l in result.stdout.strip().split('\n') if l.strip()] if result.stdout.strip() else []
            checks['playwright_pods'] = len(pod_lines)
        else:
            try:
                pods = self.v1.list_namespaced_pod(
                    namespace=self.namespace,
                    label_selector="app=playwright-agent"
                )
                checks['playwright_pods'] = len(pods.items)
            except Exception:
                checks['playwright_pods'] = -1
        result = run_cli_command(["kubectl", "get", "pvc", "-l", "app=playwright-agent", "-o", "name"], namespace=self.namespace)
        pvc_lines = [l for l in result.stdout.strip().split('\n') if l.strip()] if result.stdout.strip() else []
        checks['playwright_pvcs'] = len(pvc_lines)
        result = run_cli_command(["kubectl", "get", "pdb", "playwright-agent-pdb", "-o", "name"], namespace=self.namespace)
        pdb_lines = [l for l in result.stdout.strip().split('\n') if l.strip()] if result.stdout.strip() else []
        checks['conflicting_pdbs'] = len(pdb_lines)
        checks['is_clean'] = (
            checks['helm_test_releases'] == 0 and
            checks['playwright_pods'] == 0 and
            checks['playwright_pvcs'] == 0 and
            checks['conflicting_pdbs'] == 0
        )
        return checks

# --- Cleanup API Endpoints ---
cleanup_handler = ClusterCleanup()

@app.route('/api/cleanup/all', methods=['POST'])
@require_api_key
def cleanup_all():
    CLEANUP_OPERATIONS_TOTAL.labels(resource_type='all', result='attempted').inc()
    data = request.json or {}
    dry_run = data.get('dry_run', False)
    results = cleanup_handler.cleanup_all(dry_run=dry_run)
    return jsonify(results)

@app.route('/api/cleanup/release/<release_name>', methods=['DELETE'])
@require_api_key
def cleanup_release(release_name):
    if not is_valid_release_name(release_name):
        return jsonify({'error': 'Invalid release name'}), 400
    CLEANUP_OPERATIONS_TOTAL.labels(resource_type='release', result='attempted').inc()
    data = request.json or {}
    dry_run = data.get('dry_run', False)
    results = cleanup_handler.cleanup_specific_release(release_name, dry_run=dry_run)
    return jsonify(results)

@app.route('/api/cleanup/stuck', methods=['POST'])
@require_api_key
def cleanup_stuck():
    CLEANUP_OPERATIONS_TOTAL.labels(resource_type='stuck', result='attempted').inc()
    data = request.json or {}
    dry_run = data.get('dry_run', False)
    results = {
        'pvcs': cleanup_handler.cleanup_orphaned_pvcs(dry_run),
        'pdbs': cleanup_handler.cleanup_pdbs(dry_run)
    }
    return jsonify(results)

@app.route('/api/cleanup/reset', methods=['POST'])
@require_api_key
def reset_cluster():
    CLEANUP_OPERATIONS_TOTAL.labels(resource_type='reset', result='attempted').inc()
    data = request.json or {}
    dry_run = data.get('dry_run', False)
    results = cleanup_handler.reset_cluster_state(dry_run=dry_run)
    return jsonify(results)

@app.route('/api/cleanup/verify', methods=['GET'])
def verify_clean_state():
    results = cleanup_handler.verify_clean_state()
    return jsonify(results)

@app.route('/api/cleanup/preflight', methods=['GET'])
def preflight_check():
    state = cleanup_handler.verify_clean_state()
    if state['is_clean']:
        return jsonify({
            'ready': True,
            'message': 'Cluster is clean and ready for tests',
            'state': state
        })
    else:
        issues = []
        if state['helm_test_releases'] > 0:
            issues.append(f"{state['helm_test_releases']} test releases exist")
        if state['playwright_pods'] > 0:
            issues.append(f"{state['playwright_pods']} playwright pods exist")
        if state['playwright_pvcs'] > 0:
            issues.append(f"{state['playwright_pvcs']} PVCs exist")
        if state['conflicting_pdbs'] > 0:
            issues.append("Conflicting PDB exists")
        return jsonify({
            'ready': False,
            'message': 'Cluster has leftover resources',
            'issues': issues,
            'state': state,
            'suggested_action': 'POST to /api/cleanup/reset'
        })
# Status fallback logic
def get_status():
    if not native_k8s_client_enabled():
        return get_status_kubectl()
    try:
        return k8s_monitor.get_detailed_summary()
    except Exception as e:
        logging.warning(f"Native client failed, falling back to kubectl: {e}")
        return get_status_kubectl()

def get_status_kubectl():
    """Fallback using kubectl command"""
    pod_result = run_cli_command(["kubectl", "get", "pods", "-l", "app=playwright-agent", "-o", "name"])
    lines = [l for l in pod_result.stdout.strip().split('\n') if l.strip()] if pod_result.stdout.strip() else []
    job_result = run_cli_command(["kubectl", "get", "jobs", "-l", "app=playwright-agent", "-o", "json"])
    requested_total = len(lines)
    if job_result.returncode == 0 and job_result.stdout.strip():
        try:
            jobs = json.loads(job_result.stdout).get('items', [])
            requested_total = sum((job.get('spec', {}) or {}).get('completions') or 0 for job in jobs) or requested_total
        except Exception:
            pass
    return {
        "total": int(requested_total),
        "activePods": len(lines),
        "success": 0,
        "running": 0,
        "errors": 0,
        "pending": int(requested_total)
    }


# --- Advanced Diagnostic Classes ---

class DeploymentDiagnostics:
    """Diagnose why deployments are failing"""
    def __init__(self):
        self.v1 = v1
    def diagnose_deployment_failure(self, release_name):
        diagnosis = {'release': release_name, 'issues': [], 'suggestions': []}
        image_issues = self.check_image_pull_errors(release_name)
        if image_issues:
            diagnosis['issues'].append({'type': 'ImagePullBackOff', 'details': image_issues, 'fix': 'Check image name and registry credentials'})
        quota_issues = self.check_resource_quotas()
        if quota_issues:
            diagnosis['issues'].append({'type': 'ResourceQuotaExceeded', 'details': quota_issues, 'fix': 'Reduce resource requests or increase quota'})
        scheduling_issues = self.check_scheduling_failures(release_name)
        if scheduling_issues:
            diagnosis['issues'].append({'type': 'SchedulingFailed', 'details': scheduling_issues, 'fix': 'Check node resources and tolerations'})
        pvc_issues = self.check_pvc_binding_issues(release_name) if hasattr(self, 'check_pvc_binding_issues') else []
        if pvc_issues:
            diagnosis['issues'].append({'type': 'PVCNotBound', 'details': pvc_issues, 'fix': 'Check storage class and PV availability'})
        return diagnosis
    def check_image_pull_errors(self, release_name):
        pods = self.v1.list_namespaced_pod(namespace="default", label_selector=f"release={release_name}")
        issues = []
        for pod in pods.items:
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if cs.state and cs.state.waiting:
                        reason = cs.state.waiting.reason
                        if reason in ['ImagePullBackOff', 'ErrImagePull']:
                            issues.append({'pod': pod.metadata.name, 'container': cs.name, 'image': cs.image, 'reason': reason, 'message': cs.state.waiting.message})
        return issues
    def check_resource_quotas(self):
        # Placeholder: implement as needed
        return []
    def check_scheduling_failures(self, release_name):
        pods = self.v1.list_namespaced_pod(namespace="default", label_selector=f"release={release_name}", field_selector="status.phase=Pending")
        issues = []
        for pod in pods.items:
            if pod.status.conditions:
                for condition in pod.status.conditions:
                    if condition.type == 'PodScheduled' and condition.status == 'False':
                        issues.append({'pod': pod.metadata.name, 'reason': condition.reason, 'message': condition.message})
        return issues

class PerformanceDiagnostics:
    """Diagnose performance issues"""
    def __init__(self):
        self.v1 = v1
    def analyze_pod_performance(self, release_name):
        diagnosis = {'release': release_name, 'high_cpu_pods': [], 'high_memory_pods': [], 'restart_loops': [], 'slow_startup': []}
        pods = self.v1.list_namespaced_pod(namespace="default", label_selector=f"release={release_name}")
        for pod in pods.items:
            pod_name = pod.metadata.name
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if cs.restart_count > 3:
                        diagnosis['restart_loops'].append({'pod': pod_name, 'container': cs.name, 'restarts': cs.restart_count})
            if pod.status.start_time:
                startup_time = datetime.now(timezone.utc) - pod.status.start_time
                if (pod.status.phase == 'Pending' and startup_time.total_seconds() > 60):
                    diagnosis['slow_startup'].append({'pod': pod_name, 'pending_duration': str(startup_time)})
        try:
            metrics = self.get_pod_metrics(release_name)
            for metric in metrics:
                if metric['cpu_usage'] > 0.8:
                    diagnosis['high_cpu_pods'].append(metric)
                if metric['memory_usage'] > 0.8:
                    diagnosis['high_memory_pods'].append(metric)
        except:
            diagnosis['metrics_available'] = False
        return diagnosis
    def get_pod_metrics(self, release_name):
        result = subprocess.run(
            ["kubectl", "top", "pods", "-l", f"release={release_name}", "--no-headers"],
            capture_output=True, text=True
        )
        metrics = []
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split()
                if len(parts) >= 3:
                    metrics.append({'pod': parts[0], 'cpu_usage': self.parse_cpu(parts[1]), 'memory_usage': self.parse_memory(parts[2])})
        return metrics
    def parse_cpu(self, cpu_str):
        # Example: '100m' or '1'
        if cpu_str.endswith('m'):
            return float(cpu_str[:-1]) / 1000
        return float(cpu_str)
    def parse_memory(self, mem_str):
        # Example: '128Mi', '1Gi'
        if mem_str.endswith('Mi'):
            return float(mem_str[:-2]) / 1024
        if mem_str.endswith('Gi'):
            return float(mem_str[:-2])
        return float(mem_str)

class NetworkDiagnostics:
    """Diagnose network connectivity issues"""
    def __init__(self):
        self.v1 = v1
    def diagnose_connectivity(self, release_name):
        diagnosis = {'release': release_name, 'dns_resolution': {}, 'service_connectivity': {}, 'external_connectivity': {}}
        test_pod = self.get_first_running_pod(release_name)
        if test_pod:
            services = ['backend', 'results-api', 'postgres']
            for svc in services:
                result = self.exec_in_pod(test_pod, f"nslookup {svc} 2>/dev/null || echo 'FAILED'")
                diagnosis['dns_resolution'][svc] = 'FAILED' not in result
            endpoints = [('backend:5001', '/health'), ('results-api:5000', '/health')]
            for endpoint, path in endpoints:
                result = self.exec_in_pod(test_pod, f"wget -q -O- --timeout=3 http://{endpoint}{path} 2>/dev/null && echo 'OK' || echo 'FAILED'")
                diagnosis['service_connectivity'][endpoint] = 'OK' in result
            result = self.exec_in_pod(test_pod, "wget -q -O- --timeout=3 https://example.com 2>/dev/null && echo 'OK' || echo 'FAILED'")
            diagnosis['external_connectivity']['internet'] = 'OK' in result
        return diagnosis
    def get_first_running_pod(self, release_name):
        pods = self.v1.list_namespaced_pod(namespace="default", label_selector=f"release={release_name}")
        for pod in pods.items:
            if pod.status.phase == 'Running':
                return pod.metadata.name
        return None
    def exec_in_pod(self, pod_name, command):
        if not re.match(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$', pod_name):
            return 'FAILED: invalid pod name'
        result = subprocess.run(
            ["kubectl", "exec", pod_name, "--", "sh", "-c", command],
            capture_output=True, text=True
        )
        return result.stdout.strip()

class JobProgressDiagnostics:
    """Track and predict job completion"""
    def __init__(self):
        self.v1 = v1
        self.batch_v1 = batch_v1
    def analyze_job_progress(self, release_name):
        job_name = f"{release_name}-agent"
        try:
            job = self.batch_v1.read_namespaced_job(job_name, "default")
            pods = self.v1.list_namespaced_pod(namespace="default", label_selector=f"job-name={job_name}")
            total = job.spec.completions or 0
            succeeded = job.status.succeeded or 0
            failed = job.status.failed or 0
            active = job.status.active or 0
            completion_rate = 0
            estimated_completion = None
            if succeeded > 0 and job.status.start_time:
                elapsed = datetime.now(timezone.utc) - job.status.start_time
                completion_rate = succeeded / elapsed.total_seconds()
                if completion_rate > 0:
                    remaining = total - succeeded
                    seconds_remaining = remaining / completion_rate
                    estimated_completion = datetime.now(timezone.utc) + timedelta(seconds=seconds_remaining)
            pod_durations = []
            for pod in pods.items:
                if pod.status.phase == 'Succeeded':
                    if pod.status.start_time and pod.status.container_statuses:
                        for cs in pod.status.container_statuses:
                            if cs.state and cs.state.terminated:
                                duration = cs.state.terminated.finished_at - cs.state.terminated.started_at
                                pod_durations.append(duration.total_seconds())
            avg_duration = sum(pod_durations) / len(pod_durations) if pod_durations else 0
            return {
                'job_name': job_name,
                'progress': {
                    'total': total,
                    'succeeded': succeeded,
                    'failed': failed,
                    'active': active,
                    'percent': (succeeded / total * 100) if total > 0 else 0
                },
                'prediction': {
                    'completion_rate': f"{completion_rate:.2f} pods/sec",
                    'estimated_completion': estimated_completion.isoformat() if estimated_completion else None,
                    'average_pod_duration': f"{avg_duration:.1f}s"
                },
                'health': {
                    'failure_rate': (failed / (succeeded + failed) * 100) if (succeeded + failed) > 0 else 0,
                    'is_healthy': failed < (succeeded * 0.1) if succeeded else True
                }
            }
        except ApiException:
            return {'error': 'Job not found'}

class ClusterHealthDiagnostics:
    """Overall cluster health checks"""
    def __init__(self):
        self.v1 = v1
    def cluster_health_check(self):
        health = {'nodes': self.check_nodes(), 'pods': self.check_cluster_pods(), 'storage': self.check_storage(), 'networking': self.check_networking() if hasattr(self, 'check_networking') else {}, 'overall': 'healthy'}
        issues = []
        if health['nodes']['not_ready'] > 0:
            issues.append(f"{health['nodes']['not_ready']} nodes not ready")
        if health['storage']['failed_pvcs'] > 0:
            issues.append(f"{health['storage']['failed_pvcs']} failed PVCs")
        if issues:
            health['overall'] = 'degraded'
            health['issues'] = issues
        return health
    def check_nodes(self):
        nodes = self.v1.list_node()
        ready = 0
        not_ready = 0
        node_details = []
        for node in nodes.items:
            for condition in node.status.conditions:
                if condition.type == 'Ready':
                    if condition.status == 'True':
                        ready += 1
                    else:
                        not_ready += 1
                        node_details.append({'name': node.metadata.name, 'reason': condition.reason, 'message': condition.message})
        return {'total': ready + not_ready, 'ready': ready, 'not_ready': not_ready, 'details': node_details}
    def check_storage(self):
        pvcs = self.v1.list_namespaced_persistent_volume_claim(namespace="default")
        bound = 0
        pending = 0
        failed = 0
        for pvc in pvcs.items:
            if pvc.status.phase == 'Bound':
                bound += 1
            elif pvc.status.phase == 'Pending':
                pending += 1
            else:
                failed += 1
        return {'total': bound + pending + failed, 'bound': bound, 'pending': pending, 'failed_pvcs': failed}
    def check_cluster_pods(self):
        pods = self.v1.list_pod_for_all_namespaces()
        running = sum(1 for pod in pods.items if pod.status.phase == 'Running')
        failed = sum(1 for pod in pods.items if pod.status.phase == 'Failed')
        pending = sum(1 for pod in pods.items if pod.status.phase == 'Pending')
        return {'running': running, 'failed': failed, 'pending': pending, 'total': len(pods.items)}

class CostDiagnostics:
    """Estimate resource usage and costs"""
    def __init__(self):
        self.v1 = v1
    def estimate_test_cost(self, release_name):
        pods = self.v1.list_namespaced_pod(namespace="default", label_selector=f"release={release_name}")
        total_cpu_requests = 0
        total_memory_requests = 0
        total_cpu_limits = 0
        total_memory_limits = 0
        pod_seconds = 0
        for pod in pods.items:
            for container in pod.spec.containers:
                if container.resources:
                    if container.resources.requests:
                        total_cpu_requests += self.parse_cpu(container.resources.requests.get('cpu', '0'))
                        total_memory_requests += self.parse_memory(container.resources.requests.get('memory', '0'))
                    if container.resources.limits:
                        total_cpu_limits += self.parse_cpu(container.resources.limits.get('cpu', '0'))
                        total_memory_limits += self.parse_memory(container.resources.limits.get('memory', '0'))
            if pod.status.start_time and pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if cs.state and cs.state.terminated:
                        duration = (cs.state.terminated.finished_at - pod.status.start_time).total_seconds()
                        pod_seconds += duration
        return {
            'release': release_name,
            'pods_analyzed': len(pods.items),
            'total_pod_seconds': pod_seconds,
            'pod_hours': pod_seconds / 3600,
            'resource_requests': {
                'cpu_cores': total_cpu_requests,
                'memory_gb': total_memory_requests / (1024**3)
            },
            'resource_limits': {
                'cpu_cores': total_cpu_limits,
                'memory_gb': total_memory_limits / (1024**3)
            },
            'estimated_cost': {
                'compute': f"${pod_seconds / 3600 * 0.05:.2f}",
                'memory': f"${total_memory_requests / (1024**3) * 0.01:.2f}"
            }
        }

# --- Advanced Diagnostic Endpoints ---
@app.route('/api/diagnostics/deployment/<release>', methods=['GET'])
def diagnose_deployment(release):
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='deployment').inc()
    diag = DeploymentDiagnostics()
    return jsonify(diag.diagnose_deployment_failure(release))

@app.route('/api/diagnostics/performance/<release>', methods=['GET'])
def diagnose_performance(release):
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='performance').inc()
    diag = PerformanceDiagnostics()
    return jsonify(diag.analyze_pod_performance(release))

@app.route('/api/diagnostics/network/<release>', methods=['GET'])
def diagnose_network(release):
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='network').inc()
    diag = NetworkDiagnostics()
    return jsonify(diag.diagnose_connectivity(release))

@app.route('/api/diagnostics/progress/<release>', methods=['GET'])
def diagnose_progress(release):
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='progress').inc()
    diag = JobProgressDiagnostics()
    return jsonify(diag.analyze_job_progress(release))

@app.route('/api/diagnostics/cluster', methods=['GET'])
def diagnose_cluster():
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='cluster').inc()
    diag = ClusterHealthDiagnostics()
    return jsonify(diag.cluster_health_check())

@app.route('/api/diagnostics/cost/<release>', methods=['GET'])
def diagnose_cost(release):
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='cost').inc()
    diag = CostDiagnostics()
    return jsonify(diag.estimate_test_cost(release))
# --- EnhancedAgentCLI ---

class EnhancedAgentCLI:
    def choose_preset_menu_detailed(self):
        """Show preset options with full details and a Back option"""
        preset = questionary.select(
            "Choose preset:",
            choices=[
                questionary.Choice(
                    title="tiny   │ 5 agents │ 2 parallel │ ~10s │ Quick sanity check",
                    value="tiny"
                ),
                questionary.Choice(
                    title="small  │ 10 agents │ 5 parallel │ ~30s │ Dev testing",
                    value="small"
                ),
                questionary.Choice(
                    title="medium │ 50 agents │ 10 parallel │ ~2m │ Integration testing",
                    value="medium"
                ),
                questionary.Choice(
                    title="large  │ 100 agents │ 20 parallel │ ~5m │ Performance testing",
                    value="large"
                ),
                questionary.Choice(
                    title="xlarge │ 500 agents │ 50 parallel │ ~15m │ Stress testing",
                    value="xlarge"
                ),
                questionary.Separator(),
                questionary.Choice(
                    title="CUSTOM - Set your own values",
                    value="custom"
                ),
                questionary.Separator(),
                questionary.Choice(
                    title="Back",
                    value="back"
                ),
            ]
        ).ask()
        return preset

    def quick_test_menu_fancy(self):
        """Questionary-powered menu with descriptions and Back option (no emojis)"""
        import questionary
        import time

        choices = [
            questionary.Choice(
                title="Tiny   - 5 agents, 2 parallel (~10s)",
                value={"name": "tiny", "completions": 5, "parallelism": 2, "persona": "impatient"}
            ),
            questionary.Choice(
                title="Small  - 10 agents, 5 parallel (~30s)",
                value={"name": "small", "completions": 10, "parallelism": 5, "persona": "impatient"}
            ),
            questionary.Choice(
                title="Medium - 50 agents, 10 parallel (~2m)",
                value={"name": "medium", "completions": 50, "parallelism": 10, "persona": "strategic"}
            ),
            questionary.Choice(
                title="Large  - 100 agents, 20 parallel (~5m)",
                value={"name": "large", "completions": 100, "parallelism": 20, "persona": "browser"}
            ),
            questionary.Choice(
                title="XL     - 500 agents, 50 parallel (~15m)",
                value={"name": "xl", "completions": 500, "parallelism": 50, "persona": "browser"}
            ),
            questionary.Separator(),
            questionary.Choice(
                title="Custom - Choose your own settings",
                value="custom"
            ),
            questionary.Separator(),
            questionary.Choice(
                title="Back to main menu",
                value="back"
            ),
        ]

        selected = questionary.select(
            "Select test preset:",
            choices=choices
        ).ask()

        if selected == "back":
            return
        elif selected == "custom":
            if hasattr(self, 'custom_test_menu'):
                self.custom_test_menu()
            else:
                self.console.print("[yellow]Custom test menu not implemented.[/yellow]")
                input("\nPress Enter to continue...")
        else:
            name = f"{selected['name']}-{int(time.time())}"
            if hasattr(self, 'quick_test'):
                self.quick_test(name, selected['completions'], selected['parallelism'], selected['persona'])
            else:
                self.console.print(f"[yellow]Would run quick test: {name} ({selected})[/yellow]")
                input("\nPress Enter to continue...")

    def __init__(self):
        self.console = Console()
        self.style = Style([
            ('qmark', 'fg:#00ff00 bold'),
            ('question', 'bold'),
            ('pointer', 'fg:#00ff00 bold'),
        ])

    def show_banner(self):
        """Rich banner"""
        self.console.print(Panel.fit(
            "[bold cyan]🎮 AGENT CONTROL CENTER[/bold cyan]\n"
            "[dim]Interactive CLI v2.0[/dim]",
            border_style="cyan"
        ))

    def clear_screen(self):
        os.system('clear' if os.name == 'posix' else 'cls')

    def show_rich_dashboard(self):
        self.console.print(Panel("[bold green]Dashboard coming soon![/bold green]", border_style="green"))

    def interactive_menu(self):
        import questionary
        self.clear_screen()
        self.show_banner()
        while True:
            action = questionary.select(
                "What would you like to do?",
                choices=[
                    "Show Dashboard",
                    "Quick Test",
                    "Helm Operations",
                    "Watch Pods",
                    questionary.Separator(),
                    "Exit"
                ],
                style=self.style
            ).ask()
            if action == "Exit":
                self.console.print("[green]Goodbye![/green]")
                break
            elif action == "Show Dashboard":
                self.show_rich_dashboard()
                input("\nPress Enter to continue...")
            elif action == "Quick Test":
                self.quick_test_menu_fancy()
            elif action == "Helm Operations":
                self.console.print("[yellow]Helm Operations coming soon![yellow]")
                input("\nPress Enter to continue...")
            elif action == "Watch Pods":
                self.console.print("[yellow]Watch Pods coming soon![yellow]")
                input("\nPress Enter to continue...")


@app.route('/')
@app.route('/dashboard')
def dashboard():
    """Serve the simulation dashboard."""
    return send_from_directory(_DASHBOARD_DIR, 'index.html')

@app.route('/favicon.svg')
@app.route('/favicon.ico')
def favicon():
    """Serve the dashboard favicon so browsers don't get a 404 and show a letter placeholder."""
    return send_from_directory(_DASHBOARD_DIR, 'favicon.svg')

# Native Kubernetes clients are initialized near the top of the module.

# Cache for performance
_cache = {
    'data': None,
    'timestamp': None,
    'ttl': 5  # seconds
}

_updater_running = True

# --- Activity Log ---
_activity_log: list = []
_agent_results: list = []
_agent_states: dict = {}   # pod -> {timestamp, type, details, username} — never trimmed
_previous_pod_states: dict = {}
MAX_LOG_ENTRIES = 200
MAX_AGENT_RESULTS = 500

# ── Event ingestion queue ────────────────────────────────────────────────────
# HTTP request handlers put events here immediately (non-blocking) and return.
# A single consumer thread drains the queue, deduplicates bursts, and writes
# to _activity_log under a lock — the same admission-control pattern Kueue
# uses at the Kubernetes layer, applied here in-process.
_event_queue: queue.Queue = queue.Queue(maxsize=2000)
_log_lock = threading.Lock()

# Cumulative event totals — accumulate for the lifetime of the server process
# so the dashboard stats bar stays meaningful after pods are cleaned up.
_event_totals: dict = {
    # K8s pod lifecycle
    'started': 0, 'completed': 0, 'failed': 0, 'pending': 0,
    # Agent login
    'registered': 0, 'logged_in': 0, 'agent_done': 0,
    # Probe
    'probe_start': 0, 'probe_get': 0, 'probe_error': 0, 'probe_done': 0,
    # Transfer-stacker
    'asset_created': 0, 'transfer_completed': 0, 'transfer_started': 0,
    'conflict_detected': 0, 'consistency_check': 0,
}


def _reset_run_state() -> None:
    """Clear run-scoped dashboard state before starting a new simulation."""
    with _log_lock:
        _activity_log.clear()
        _agent_results.clear()
        _agent_states.clear()
        _previous_pod_states.clear()
        for key in _event_totals:
            _event_totals[key] = 0

    while True:
        try:
            _event_queue.get_nowait()
        except queue.Empty:
            break
        else:
            _event_queue.task_done()

    _cache['data'] = None
    _cache['timestamp'] = None


def _set_active_test(payload: Optional[dict] = None) -> dict:
    data = dict(_EMPTY_ACTIVE_TEST)
    if payload:
        data.update({
            'target_url': str(payload.get('target_url') or ''),
            'probe_mode': str(payload.get('probe_mode') or ''),
            'test_name': str(payload.get('test_name') or ''),
            'completions': str(payload.get('completions') or ''),
            'parallelism': str(payload.get('parallelism') or ''),
        })
        if any(data.values()):
            _LAST_ACTIVE_TEST.update(data)
            _persist_last_active_test(_LAST_ACTIVE_TEST)
    SIMULATION_ACTIVE_TEST.info(data)
    return data


def _log_consumer():
    """Drain _event_queue, deduplicate bursts, write to _activity_log under lock."""
    # Track the last entry written per pod to suppress identical bursts.
    # Key: (pod_full, event_type, details)  Value: epoch second of last write.
    _last_seen: dict = {}
    DEDUP_WINDOW = 1  # seconds — collapse identical (pod, type, details) within this window

    while True:
        try:
            entry = _event_queue.get(timeout=1)
        except queue.Empty:
            continue

        key = (entry['pod_full'], entry['type'], entry['details'])
        now_sec = int(time.time())
        if _last_seen.get(key) == now_sec:
            # Exact duplicate within the same second — drop but still drain
            _event_queue.task_done()
            continue
        _last_seen[key] = now_sec

        # Evict stale dedup entries every ~1000 events to bound memory
        if len(_last_seen) > 1000:
            cutoff = now_sec - DEDUP_WINDOW * 10
            _last_seen = {k: v for k, v in _last_seen.items() if v > cutoff}

        with _log_lock:
            _activity_log.append(entry)
            if len(_activity_log) > MAX_LOG_ENTRIES:
                del _activity_log[:len(_activity_log) - MAX_LOG_ENTRIES]
            et = entry['type']
            if et in _event_totals:
                _event_totals[et] += 1

        _event_queue.task_done()


_log_consumer_thread = threading.Thread(target=_log_consumer, daemon=True, name='log-consumer')
_log_consumer_thread.start()


def add_activity_log(event_type: str, pod_name: str, details: str = None):
    """Enqueue an event for the consumer thread — returns immediately."""
    short_name = pod_name[:20] + '...' if len(pod_name) > 23 else pod_name
    entry = {
        'timestamp': datetime.now(timezone.utc).strftime('%H:%M:%S'),
        'type': event_type,
        'pod': short_name,
        'pod_full': pod_name,
        'details': details,
    }
    try:
        _event_queue.put_nowait(entry)
    except queue.Full:
        # Queue saturated — drop oldest and retry once so we never block a request thread
        try:
            _event_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _event_queue.put_nowait(entry)
        except queue.Full:
            pass


def detect_state_changes(pods):
    """Compare current pod states against previous snapshot and log changes."""
    for pod in pods:
        pod_name = pod.metadata.name
        phase = pod.status.phase or 'Unknown'
        previous = _previous_pod_states.get(pod_name)

        if previous != phase:
            if phase == 'Running':
                add_activity_log('started', pod_name)
            elif phase == 'Succeeded':
                duration = None
                if pod.status.start_time:
                    elapsed = datetime.now(timezone.utc) - pod.status.start_time.replace(tzinfo=timezone.utc)
                    duration = f"{elapsed.total_seconds():.1f}s"
                add_activity_log('completed', pod_name, duration)
            elif phase == 'Pending':
                add_activity_log('pending', pod_name, 'waiting for resources')
            elif phase == 'Failed':
                reason = None
                if pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        if cs.state and cs.state.terminated and cs.state.terminated.reason:
                            reason = cs.state.terminated.reason
                            break
                add_activity_log('failed', pod_name, reason)
            _previous_pod_states[pod_name] = phase

class HelmClient:
    """Helm client wrapper for test orchestration"""
    
    def __init__(self, namespace: str = "default", dry_run: bool = False):
        self.namespace = namespace
        self.dry_run = dry_run
    
    # Directories to search for helm / kubectl beyond what the service process
    # inherited.  Homebrew on Apple-silicon installs to /opt/homebrew/bin which
    # is often absent from the PATH of GUI-launched or supervisor-managed
    # processes.
    _EXTRA_BIN_DIRS = [
        '/opt/homebrew/bin',
        '/usr/local/bin',
        '/usr/bin',
        '/bin',
    ]

    @classmethod
    def _augmented_env(cls) -> dict:
        env = os.environ.copy()
        current = env.get('PATH', '')
        parts = current.split(':')
        extra = ':'.join(d for d in cls._EXTRA_BIN_DIRS if d not in parts)
        env['PATH'] = f"{extra}:{current}" if extra else current
        return env

    @classmethod
    def _resolve_binary(cls, name: str) -> str:
        found = shutil.which(name, path=cls._augmented_env()['PATH'])
        return found if found else name

    def _run(self, cmd: List[str]) -> Dict[str, Any]:
        """Run a helm command safely (never uses shell=True)"""
        if self.dry_run:
            logger.info(f"[DRY RUN] {' '.join(cmd)}")
            return {"success": True, "dry_run": True, "stdout": "", "stderr": ""}

        # Resolve the binary so the call succeeds even when /opt/homebrew/bin
        # is absent from the service process PATH (common on macOS).
        cmd = [self._resolve_binary(cmd[0]), *cmd[1:]]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, shell=False)
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except FileNotFoundError as exc:
            binary = cmd[0] if cmd else "command"
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Required command '{binary}' was not found: {exc}",
                "returncode": 127,
            }
        except Exception as exc:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Failed to run command '{' '.join(cmd)}': {exc}",
                "returncode": 1,
            }
    
    def install(self, name: str, chart: str, values: Dict = None, wait: bool = False) -> Dict:
        """Install a Helm chart with validated inputs.

        Any existing release with the same name is removed first
        (``--ignore-not-found``) so callers never hit a
        "cannot re-use a name that is still in use" error.
        """
        if not is_valid_release_name(name):
            return {"success": False, "stderr": f"Invalid release name: {name}", "stdout": "", "returncode": 1}

        # Always clean up a previous release to avoid conflicts
        self._run(["helm", "uninstall", name, "-n", self.namespace, "--ignore-not-found"])
        time.sleep(2)  # Let K8s GC the old resources

        cmd = ["helm", "install", name, chart, "-n", self.namespace]
        
        if wait:
            cmd.append("--wait")
        
        if values:
            for key, value in values.items():
                cmd.extend(["--set", f"{key}={value}"])
        
        result = self._run(cmd)
        HELM_OPERATIONS_TOTAL.labels(
            operation='install',
            status='success' if result['success'] else 'failure',
        ).inc()
        return result
    
    def uninstall(self, name: str) -> Dict:
        """Uninstall a Helm release with validated name"""
        if not is_valid_release_name(name):
            return {"success": False, "stderr": f"Invalid release name: {name}", "stdout": "", "returncode": 1}

        cmd = ["helm", "uninstall", name, "-n", self.namespace]
        result = self._run(cmd)
        HELM_OPERATIONS_TOTAL.labels(
            operation='uninstall',
            status='success' if result['success'] else 'failure',
        ).inc()
        return result
    
    def list_releases(self, filter_pattern: str = "playwright") -> List[Dict]:
        """List Helm releases"""
        result = self._run(["helm", "list", "-n", self.namespace, "-o", "json"])
        if not result["success"] or not result["stdout"]:
            return []

        try:
            releases = json.loads(result["stdout"])
        except Exception:
            return []

        if not filter_pattern:
            return releases
        if filter_pattern == "playwright":
            return [
                release for release in releases
                if is_playwright_release(release.get("name", ""), self.namespace)
            ]

        pattern = re.compile(filter_pattern, re.IGNORECASE)
        return [release for release in releases if pattern.search(release.get("name", ""))]
    
    def get_values(self, name: str) -> Dict:
        """Get values for a release"""
        cmd = ["helm", "get", "values", name, "-n", self.namespace, "-o", "json"]
        result = self._run(cmd)
        
        if result["success"] and result["stdout"]:
            try:
                return json.loads(result["stdout"])
            except:
                return {}
        return {}

# K8sNativeMonitor is provided by simulation_service_tool.services.k8s_native (imported above).

# Initialize clients
k8s_monitor = K8sNativeMonitor()
helm_client = HelmClient()

class TestController:
    """Controls test execution"""
    
    @staticmethod
    def run_test(name: str, completions: int, parallelism: int, persona: str,
                 workers: int = 1, wait: bool = False,
                 image_repository: Optional[str] = None,
                 image_tag: Optional[str] = None,
                 replica_count: Optional[int] = None,
                 shard_total: Optional[int] = None,
                 request_memory: Optional[str] = None,
                 request_cpu: Optional[str] = None,
                 limit_memory: Optional[str] = None,
                 limit_cpu: Optional[str] = None,
                 backoff_limit: Optional[int] = None,
                 ttl_seconds_after_finished: Optional[int] = None,
                 command_override: Optional[str] = None,
                 kueue: Optional[bool] = None,
                 probe_mode: Optional[str] = None,
                 probe_url: Optional[str] = None) -> Dict:
        """Run a test with specified parameters"""
        
        values = {
            'completions': completions,
            'parallelism': parallelism,
            'persona': persona,
            'workersPerPod': workers
        }
        if replica_count is not None:
            values['replicaCount'] = replica_count
        if shard_total is not None:
            values['shardTotal'] = shard_total
        if request_memory:
            values['resources.requests.memory'] = request_memory
        if request_cpu:
            values['resources.requests.cpu'] = request_cpu
        if limit_memory:
            values['resources.limits.memory'] = limit_memory
        if limit_cpu:
            values['resources.limits.cpu'] = limit_cpu
        if backoff_limit is not None:
            values['backoffLimit'] = backoff_limit
        if ttl_seconds_after_finished is not None:
            values['ttlSecondsAfterFinished'] = ttl_seconds_after_finished
        # Default to the local custom agent image so pods never fall back to the
        # upstream mcr.microsoft.com/playwright base image which has no run.py.
        if not image_repository:
            image_repository = 'playwright-agent'
        if not image_tag:
            image_tag = 'latest'
        if image_repository:
            values['image.repository'] = image_repository
        if image_tag:
            values['image.tag'] = image_tag
        if command_override:
            values['commandOverride'] = command_override
        if image_repository == 'playwright-agent':
            # Transactional agents need to reach the C# backend from inside the cluster
            values['targetUrl'] = 'http://host.docker.internal:5001'
            values['simApi'] = 'http://host.docker.internal:5002/api/simulation'
            values['backendApi'] = 'http://host.docker.internal:5001/api/simulation/results'
            values['coordApi'] = 'http://host.docker.internal:5003/api/coordinator'
            # Use local registry so all nodes can pull without pre-loading into containerd
            values['image.repository'] = 'host.docker.internal:5050/playwright-agent'
            values['image.pullPolicy'] = 'Always'
        if kueue:
            values['kueue.enabled'] = True
            values['kueue.queueName'] = 'simulation-queue'
            logger.info(f"📋 Kueue queuing enabled for '{name}'")
        values['probeMode'] = probe_mode or 'basic'
        if probe_url:
            values['probeUrl'] = probe_url
        active_test_payload = {
            'target_url': probe_url or values.get('targetUrl', ''),
            'probe_mode': values['probeMode'],
            'test_name': name,
            'completions': str(completions),
            'parallelism': str(parallelism),
        }

        logger.info(f"🚀 Starting test '{name}' with {completions} agents (mode={values['probeMode']})")
        result = helm_client.install(name, "./helm/playwright-agent", values, wait=wait)

        if result['success']:
            _set_active_test(active_test_payload)
            if not has_request_context():
                _sync_active_test_to_service(active_test_payload)
            return {
                'success': True,
                'name': name,
                'completions': completions,
                'parallelism': parallelism,
                'persona': persona,
                'workersPerPod': workers,
                'imageRepository': image_repository,
                'imageTag': image_tag,
                'replicaCount': replica_count,
                'shardTotal': shard_total,
                'requestMemory': request_memory,
                'requestCpu': request_cpu,
                'limitMemory': limit_memory,
                'limitCpu': limit_cpu,
                'backoffLimit': backoff_limit,
                'ttlSecondsAfterFinished': ttl_seconds_after_finished,
                'commandOverride': command_override,
                'kueue': bool(kueue),
            }
        else:
            return {
                'success': False,
                'error': result['stderr']
            }
    
    @staticmethod
    def stop_test(name: str) -> Dict:
        """Stop a running test"""
        logger.info(f"🛑 Stopping test '{name}'")
        result = helm_client.uninstall(name)
        if result['success']:
            _set_active_test()
            if not has_request_context():
                _sync_active_test_to_service()
            return {'success': True, 'name': name}
        else:
            return {'success': False, 'error': result['stderr']}
    
    @staticmethod
    def list_tests() -> List[Dict]:
        """List all test releases"""
        releases = helm_client.list_releases()
        return [{
            'name': r['name'],
            'status': r['status'],
            'updated': r['updated']
        } for r in releases]


def classify_error(error_message: str) -> Dict[str, Any]:
    """Return a structured classification for known operational errors."""
    message = (error_message or "").strip()
    lower = message.lower()
    resource_match = re.search(r'([A-Za-z]+)\s+"([^"]+)"\s+in namespace', message)
    classification = {
        'kind': 'unknown',
        'summary': message or 'Unknown error',
        'details': message,
        'conflicting_release': None,
        'resource_kind': resource_match.group(1) if resource_match else None,
        'resource_name': resource_match.group(2) if resource_match else None,
        'suggestions': [],
    }

    conflict_match = re.search(r'current value is "([^"]+)"', message)
    conflicting_release = conflict_match.group(1) if conflict_match else None

    if 'cannot be imported into the current release' in lower and 'invalid ownership metadata' in lower:
        resource_kind = classification['resource_kind'] or 'resource'
        resource_name = classification['resource_name'] or 'unknown'
        classification.update({
            'kind': 'helm_resource_conflict',
            'summary': f"Helm found an existing {resource_kind} owned by another release.",
            'conflicting_release': conflicting_release,
            'suggestions': [
                f"Delete the conflicting release{f' ({conflicting_release})' if conflicting_release else ''} if it is stale.",
                f"Delete or clean up the shared {resource_kind} '{resource_name}' if the release is already gone.",
                'Clean up shared stuck resources if the resource is orphaned.',
                'Refresh cluster state before retrying the install.',
            ],
        })
    elif 'failed to establish a new connection' in lower or 'max retries exceeded' in lower or 'connection refused' in lower:
        classification.update({
            'kind': 'cluster_connection_error',
            'summary': 'The Kubernetes client could not connect to the cluster API.',
            'suggestions': [
                'Verify kubeconfig points to a reachable cluster.',
                'Use the direct kubectl/helm cleanup fallback instead of the Python client.',
                'Refresh cluster state after reconnecting.',
            ],
        })
    elif 'timed out waiting for the condition' in lower:
        classification.update({
            'kind': 'helm_timeout',
            'summary': 'Helm install timed out waiting for Kubernetes resources to become ready.',
            'suggestions': [
                'Inspect pod and job status with the watch or status command.',
                'Reduce concurrency for the next run if the cluster is saturated.',
                'Clean up stuck resources before retrying.',
            ],
        })
    elif 'required command' in lower or 'not found' in lower:
        classification.update({
            'kind': 'missing_dependency',
            'summary': 'A required CLI dependency such as helm or kubectl is missing.',
            'suggestions': [
                'Install the missing dependency and ensure it is on PATH.',
            ],
        })

    return classification


def _safe_cleanup_release(release_name: str) -> Dict[str, Any]:
    if not is_valid_release_name(release_name):
        return {'error': f'Invalid release name: {release_name}'}

    try:
        return cleanup_handler.cleanup_specific_release(release_name, dry_run=False)
    except Exception as exc:
        logger.warning(f"Falling back to direct release cleanup for {release_name}: {exc}")
        result = {
            'release': release_name,
            'helm': None,
            'pods': [],
            'pvcs': [],
            'pdbs': [],
            'jobs': [],
        }
        if not release_exists_or_has_resources(release_name):
            result['helm'] = 'not found'
            result['warning'] = f"No Helm release or owned resources found for '{release_name}'."
            return result
        helm_result = run_cli_command(["helm", "uninstall", release_name, "--ignore-not-found"])
        result['helm'] = 'uninstalled' if helm_result.returncode == 0 else helm_result.stderr.strip() or 'failed'
        for resource, key in (("pods", 'pods'), ("pvc", 'pvcs'), ("pdb", 'pdbs'), ("jobs", 'jobs')):
            names = _list_release_owned_resource_names(resource, release_name)
            for name in names:
                run_cli_command(["kubectl", "delete", resource, name, "--ignore-not-found"])
            result[key].extend(names)
        return result


def _safe_cleanup_stuck_resources() -> Dict[str, Any]:
    try:
        return cleanup_handler.cleanup_stuck_resources(dry_run=False)
    except Exception as exc:
        logger.warning(f"Falling back to direct stuck resource cleanup: {exc}")
        cleaned = []
        resources = ['pod', 'service', 'deployment', 'replicaset', 'statefulset', 'job', 'secret', 'configmap']
        for resource in resources:
            result = run_cli_command(["kubectl", "delete", resource, "-l", "app=playwright-agent", "--ignore-not-found"])
            if result.returncode == 0 and result.stdout.strip():
                cleaned.append(resource)
        pvc_result = cleanup_handler.cleanup_orphaned_pvcs(dry_run=False)
        pdb_result = cleanup_handler.cleanup_pdbs(dry_run=False)
        return {
            'count': len(cleaned),
            'resources': cleaned,
            'pvcs': pvc_result,
            'pdbs': pdb_result,
        }


def _safe_verify_cluster_state() -> Dict[str, Any]:
    try:
        return cleanup_handler.verify_clean_state()
    except Exception as exc:
        logger.warning(f"Falling back to subprocess cluster verification: {exc}")
        checks = {}
        helm_result = run_cli_command(["helm", "list", "--short"])
        releases = [r for r in helm_result.stdout.strip().split('\n') if r.strip()] if helm_result.stdout.strip() else []
        checks['helm_test_releases'] = len([r for r in releases if re.search(r'test|sim', r, re.IGNORECASE)])
        pod_result = run_cli_command(["kubectl", "get", "pods", "-l", "app=playwright-agent", "-o", "name"])
        checks['playwright_pods'] = len([l for l in pod_result.stdout.strip().split('\n') if l.strip()]) if pod_result.stdout.strip() else 0
        pvc_result = run_cli_command(["kubectl", "get", "pvc", "-l", "app=playwright-agent", "-o", "name"])
        checks['playwright_pvcs'] = len([l for l in pvc_result.stdout.strip().split('\n') if l.strip()]) if pvc_result.stdout.strip() else 0
        pdb_result = run_cli_command(["kubectl", "get", "pdb", "-o", "name"])
        checks['conflicting_pdbs'] = len([l for l in pdb_result.stdout.strip().split('\n') if 'playwright' in l or 'test' in l]) if pdb_result.stdout.strip() else 0
        checks['is_clean'] = (
            checks['helm_test_releases'] == 0 and
            checks['playwright_pods'] == 0 and
            checks['playwright_pvcs'] == 0 and
            checks['conflicting_pdbs'] == 0
        )
        return checks


def prompt_start_failure_recovery(error_message: str):
    """Offer cleanup and refresh actions after an interactive start failure."""
    if not sys.stdin.isatty():
        return

    try:
        import questionary
    except ImportError:
        return

    error_info = classify_error(error_message)
    conflicting_release = error_info.get('conflicting_release')

    print(json.dumps({
        'summary': error_info['summary'],
        'details': error_info['details'],
        'suggestions': error_info['suggestions'],
    }, indent=2))

    choices = []
    if conflicting_release:
        choices.append(
            questionary.Choice(
                title=f"Delete conflicting release '{conflicting_release}'",
                value=("delete_release", conflicting_release),
            )
        )

    choices.extend([
        questionary.Choice(
            title="Delete shared stuck resources (PVCs / PDBs / leftovers)",
            value=("cleanup_stuck", None),
        ),
        questionary.Choice(
            title="Refresh cluster state",
            value=("refresh", None),
        ),
        questionary.Choice(
            title="Return",
            value=("back", None),
        ),
    ])

    action = questionary.select(
        "Start failed. What would you like to do?",
        choices=choices,
    ).ask()

    if not action:
        return

    action_type, value = action
    if action_type == "delete_release":
        if not is_valid_release_name(value):
            print(json.dumps({"cleanup": "release", "error": f"Invalid release name: {value}"}, indent=2))
            return
        if not release_exists_or_has_resources(value):
            print(json.dumps({
                "cleanup": "release",
                "result": {
                    "release": value,
                    "helm": "not found",
                    "pods": [],
                    "pvcs": [],
                    "pdbs": [],
                    "jobs": [],
                    "warning": f"No Helm release or labeled resources found for '{value}'.",
                },
            }, indent=2))
            return
        confirm = questionary.confirm(
            f"Delete release '{value}' and its resources?",
            default=False,
        ).ask()
        if not confirm:
            return
        result = _safe_cleanup_release(value)
        print(json.dumps({"cleanup": "release", "result": result}, indent=2))
    elif action_type == "cleanup_stuck":
        result = _safe_cleanup_stuck_resources()
        print(json.dumps({"cleanup": "stuck_resources", "result": result}, indent=2))
    elif action_type == "refresh":
        state = _safe_verify_cluster_state()
        print(json.dumps({"cluster_state": state}, indent=2))


def get_release_status(release_name: str) -> Dict[str, Any]:
    """Return status for a specific Helm release using pod labels."""
    if not native_k8s_client_enabled():
        pods = _list_release_owned_resource_names("pods", release_name)
        pod_total = len(pods)
        job_result = run_cli_command(["kubectl", "get", "jobs", "-l", f"release={release_name}", "-o", "json"])
        job_stats = {}
        total = pod_total
        if job_result.returncode == 0 and job_result.stdout.strip():
            try:
                jobs = json.loads(job_result.stdout).get('items', [])
                for job in jobs:
                    metadata = job.get('metadata', {}) or {}
                    status = job.get('status', {}) or {}
                    spec = job.get('spec', {}) or {}
                    job_name = metadata.get('name', 'unknown-job')
                    job_stats[job_name] = {
                        'active': status.get('active') or 0,
                        'succeeded': status.get('succeeded') or 0,
                        'failed': status.get('failed') or 0,
                        'completions': spec.get('completions'),
                        'parallelism': spec.get('parallelism'),
                    }
                total = sum(stats['completions'] or 0 for stats in job_stats.values()) or pod_total
            except Exception:
                job_stats = {}
        return {
            'release': release_name,
            'total': total,
            'activePods': pod_total,
            'success': 0,
            'running': pod_total,
            'errors': 0,
            'pending': max(total - pod_total, 0),
            'jobs': job_stats,
            'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        }

    pods = k8s_monitor.get_pods(label_selector=f"release={release_name}")
    jobs = k8s_monitor.get_jobs(label_selector=f"release={release_name}")

    pod_total = len(pods)
    succeeded = sum(1 for pod in pods if pod.status.phase == 'Succeeded')
    running = sum(1 for pod in pods if pod.status.phase == 'Running')
    failed = sum(1 for pod in pods if pod.status.phase == 'Failed')
    pending = pod_total - succeeded - running - failed

    job_stats = {
        job.metadata.name: {
            'active': job.status.active or 0,
            'succeeded': job.status.succeeded or 0,
            'failed': job.status.failed or 0,
            'completions': job.spec.completions,
            'parallelism': job.spec.parallelism,
        }
        for job in jobs
    }
    total = sum(stats['completions'] or 0 for stats in job_stats.values()) or pod_total

    return {
        'release': release_name,
        'total': total,
        'activePods': pod_total,
        'success': succeeded,
        'running': running,
        'errors': failed,
        'pending': pending,
        'jobs': job_stats,
        'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    }


def watch_release_progress(release_name: Optional[str] = None):
    """Watch either a specific release or the global simulation summary."""
    try:
        while True:
            summary = get_release_status(release_name) if release_name else get_status()
            os.system('clear')
            print(json.dumps(summary, indent=2))
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopped watching.")


def watch_release_pods_kubectl(release_name: str):
    """Watch pods for a specific release using kubectl -w."""
    import shutil
    if not shutil.which("kubectl"):
        print("\n[WARN] kubectl not found in PATH — cannot watch pods live.")
        print(f"       Check status manually with: kubectl get pods -l release={release_name}")
        return
    print(f"\nWatching Kubernetes pods for release '{release_name}'")
    print("Press Ctrl+C to stop watching.\n")
    try:
        subprocess.run(
            ["kubectl", "get", "pods", "-w", "-l", f"release={release_name}"],
            check=False,
        )
    except FileNotFoundError:
        print("\n[WARN] kubectl not found — cannot watch pods.")
    except KeyboardInterrupt:
        print("\nStopped watching pods.")


def prompt_start_success_next_steps(result: Dict[str, Any]):
    """Offer follow-up actions after a successful interactive test start."""
    if not sys.stdin.isatty():
        return

    try:
        import questionary
    except ImportError:
        return

    release_name = result.get('name')
    action = questionary.select(
        "Test started successfully. What would you like to do next?",
        choices=[
            questionary.Choice(title="Watch this test", value="watch"),
            questionary.Choice(title="Watch pods with kubectl -w", value="watch_kubectl"),
            questionary.Choice(title="Return", value="back"),
        ],
    ).ask()

    if action == "watch" and release_name:
        watch_release_progress(release_name)
    elif action == "watch_kubectl" and release_name:
        watch_release_pods_kubectl(release_name)

# Background thread to update cache

def _background_updater():
    global _cache, _updater_running
    while _updater_running:
        try:
            data = k8s_monitor.get_detailed_summary()
            _cache['data'] = data
            _cache['timestamp'] = time.time()
            # Keep Prometheus gauges in sync so /metrics always reflects reality
            AGENT_PODS_ACTIVE.set(data.get('running', 0))
            AGENT_PODS_SUCCEEDED.set(data.get('success', 0))
            AGENT_PODS_FAILED.set(data.get('errors', 0))
            AGENT_PODS_PENDING.set(data.get('pending', 0))
            # Detect pod state changes for activity log
            pods = k8s_monitor.get_pods()
            detect_state_changes(pods)
        except Exception as e:
            logger.warning(f"Cache update failed: {e}")
        time.sleep(_cache['ttl'])

# Start background updater
updater_thread = threading.Thread(target=_background_updater, daemon=True)
updater_thread.start()

# ---------------------------------------------------------------------------
# Prometheus gauge reader — exposes gauge/counter values as plain dicts
# so the Vue dashboard can display lifetime metrics even when no pods exist.
# ---------------------------------------------------------------------------

def _read_prometheus_gauges() -> dict:
    """Snapshot current Prometheus metric values for API consumers."""
    def _gauge_val(g):
        try:
            return g._value.get()
        except Exception:
            return 0

    def _counter_total(c, label_filter=None):
        """Sum all label combinations for a Counter."""
        try:
            total = 0.0
            if hasattr(c, '_metrics'):
                for labels, metric in c._metrics.items():
                    if label_filter and not label_filter(dict(zip(c._labelnames, labels))):
                        continue
                    total += metric._value.get()
            else:
                total = c._value.get()
            return total
        except Exception:
            return 0

    def _histogram_average(h):
        try:
            total_sum = 0.0
            total_count = 0.0
            for metric in h.collect():
                for sample in getattr(metric, 'samples', []):
                    if sample.name.endswith('_sum'):
                        total_sum += float(sample.value)
                    elif sample.name.endswith('_count'):
                        total_count += float(sample.value)
            if total_count > 0:
                return round(total_sum / total_count, 1)
        except Exception:
            return None
        return None

    return {
        'active': _gauge_val(AGENT_PODS_ACTIVE),
        'succeeded': _gauge_val(AGENT_PODS_SUCCEEDED),
        'failed': _gauge_val(AGENT_PODS_FAILED),
        'pending': _gauge_val(AGENT_PODS_PENDING),
        'orchestration_starts': _counter_total(
            AGENT_ORCHESTRATION_TOTAL,
            lambda l: l.get('action') == 'start_test',
        ),
        'orchestration_stops': _counter_total(
            AGENT_ORCHESTRATION_TOTAL,
            lambda l: l.get('action') == 'stop_test',
        ),
        'helm_installs': _counter_total(
            HELM_OPERATIONS_TOTAL,
            lambda l: l.get('operation') == 'install',
        ),
        'cleanup_ops': _counter_total(CLEANUP_OPERATIONS_TOTAL),
        'kueue_pending': _gauge_val(KUEUE_PENDING_WORKLOADS),
        'kueue_admitted': _gauge_val(KUEUE_ADMITTED_WORKLOADS),
        'kueue_active': _gauge_val(KUEUE_ACTIVE),
        'avg_duration': _histogram_average(AGENT_TEST_DURATION),
        'active_test': getattr(SIMULATION_ACTIVE_TEST, '_value', {}),
        'last_target_url': _LAST_ACTIVE_TEST.get('target_url', ''),
    }


def _enrich_summary_with_prometheus(summary: dict, prom: dict | None = None) -> dict:
    """Backfill throughput stats from Prometheus when the live k8s summary is empty."""
    prom = prom or _read_prometheus_gauges()
    throughput = dict(summary.get('throughput') or {})

    rate_per_second = throughput.get('agentsPerSecond')
    rate_per_minute = throughput.get('agentsPerMinute')
    try:
        if rate_per_second in (None, '') and rate_per_minute not in (None, ''):
            throughput['agentsPerSecond'] = round(float(rate_per_minute) / 60.0, 2)
        elif rate_per_minute in (None, '') and rate_per_second not in (None, ''):
            throughput['agentsPerMinute'] = round(float(rate_per_second) * 60.0, 1)
    except (TypeError, ValueError):
        pass

    avg_duration = throughput.get('avgDuration')
    if avg_duration in (None, 0):
        avg_duration = summary.get('avg_duration')
    if avg_duration in (None, 0):
        avg_duration = prom.get('avg_duration')

    if avg_duration not in (None, 0) and summary.get('avg_duration') in (None, 0):
        summary['avg_duration'] = avg_duration

    if any(key in throughput for key in ('agentsPerSecond', 'agentsPerMinute', 'etaSeconds', 'avgDuration')):
        if avg_duration not in (None, 0) and throughput.get('avgDuration') in (None, 0):
            throughput['avgDuration'] = round(float(avg_duration), 1)
        summary['throughput'] = throughput
        return summary

    active_test = prom.get('active_test') or {}
    try:
        total_target = int(active_test.get('completions') or 0)
    except (TypeError, ValueError):
        total_target = 0

    with _log_lock:
        event_totals = dict(_event_totals)

    event_done = float((event_totals.get('agent_done') or 0) + (event_totals.get('probe_done') or 0))
    event_failed = float(event_totals.get('probe_error') or 0)
    event_started = float((event_totals.get('registered') or 0) + (event_totals.get('probe_start') or 0))
    event_active = max(event_started - event_done - event_failed, 0.0)

    prom_active = max(float(prom.get('active') or 0), 0.0)
    prom_succeeded = max(float(prom.get('succeeded') or 0), 0.0)
    prom_failed = max(float(prom.get('failed') or 0), 0.0)
    prom_pending = max(float(prom.get('pending') or 0), 0.0)

    live_from_prom = prom_active + prom_succeeded + prom_failed + prom_pending > 0
    active = prom_active if live_from_prom else event_active
    succeeded = prom_succeeded if live_from_prom else event_done
    failed = prom_failed if live_from_prom else event_failed
    pending = prom_pending if live_from_prom else 0.0
    completed_raw = succeeded + failed
    completed = min(completed_raw, float(total_target)) if total_target > 0 else completed_raw
    in_flight = active + pending

    if avg_duration not in (None, 0):
        throughput['avgDuration'] = round(float(avg_duration), 1)
    throughput['completed'] = int(round(completed))

    if total_target > 0:
        throughput['percentComplete'] = round((completed / total_target) * 100, 1)

    if avg_duration not in (None, 0) and in_flight > 0:
        rate = in_flight / float(avg_duration)
        throughput['agentsPerSecond'] = round(rate, 2)
        throughput['agentsPerMinute'] = round(rate * 60, 1)
        if total_target > 0:
            remaining = max(total_target - completed, 0.0)
            throughput['etaSeconds'] = round(remaining / rate, 1) if rate > 0 else 0
    elif total_target > 0 and completed >= total_target:
        throughput['etaSeconds'] = 0

    if throughput:
        throughput['source'] = 'prometheus'
        summary['throughput'] = throughput

    return summary


# Flask API endpoints
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'time': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')})

@app.route('/api/simulation/summary')
def simulation_summary():
    # Use cache for performance
    now = time.time()
    if _cache['data'] and _cache['timestamp'] and now - _cache['timestamp'] < _cache['ttl']:
        data = _cache['data']
    else:
        # Fallback: fetch fresh
        data = k8s_monitor.get_detailed_summary()
        _cache['data'] = data
        _cache['timestamp'] = now
    # Update Prometheus gauges from summary
    AGENT_PODS_ACTIVE.set(data.get('running', 0))
    AGENT_PODS_SUCCEEDED.set(data.get('success', 0))
    AGENT_PODS_FAILED.set(data.get('errors', 0))
    AGENT_PODS_PENDING.set(data.get('pending', 0))
    # ── Kueue queue stats ──────────────────────────────────────────────
    try:
        from simulation_service_tool.services.kueue import (
            is_kueue_installed, get_cluster_queue_status, get_local_queue_status,
        )
        kueue_installed = is_kueue_installed()
        KUEUE_ACTIVE.set(1 if kueue_installed else 0)
        if kueue_installed:
            cq = get_cluster_queue_status()
            lq = get_local_queue_status()
            pending_wl = cq.get('pending_workloads', 0) if cq.get('exists') else 0
            admitted_wl = cq.get('admitted_workloads', 0) if cq.get('exists') else 0
            KUEUE_PENDING_WORKLOADS.set(pending_wl)
            KUEUE_ADMITTED_WORKLOADS.set(admitted_wl)
            # Wait time estimation: use average job duration to estimate drain time
            avg_duration = data.get('avg_duration', 7)  # default ~7s from observed 4-10s range
            total_queued = pending_wl
            # Throughput = admitted (concurrent) slots finishing per avg_duration cycle
            throughput_per_cycle = max(admitted_wl, 1)
            estimated_drain_seconds = (total_queued / throughput_per_cycle) * avg_duration
            data['kueue'] = {
                'active': True,
                'cluster_queue': cq,
                'local_queue': lq,
                'pending_workloads': pending_wl,
                'admitted_workloads': admitted_wl,
                'estimated_drain_seconds': round(estimated_drain_seconds, 1),
            }
        else:
            KUEUE_PENDING_WORKLOADS.set(0)
            KUEUE_ADMITTED_WORKLOADS.set(0)
            data['kueue'] = {'active': False}
    except Exception:
        data['kueue'] = {'active': False}
    # Enrich response with cumulative Prometheus counters so the dashboard
    # shows lifetime totals even when no pods are currently running.
    data['prometheus'] = _read_prometheus_gauges()
    _enrich_summary_with_prometheus(data, data['prometheus'])
    # Coordinator stats (fetched from coordinator_service on port 5003)
    data['coordinator'] = _coordinator_stats_safe()
    return jsonify(data)

@app.route('/api/simulation/activity')
def simulation_activity():
    """Get recent activity log with summary counts."""
    limit = request.args.get('limit', 10, type=int)
    limit = max(1, min(limit, MAX_LOG_ENTRIES))
    cached = _cache.get('data') or {}
    with _log_lock:
        activity_snapshot = list(_activity_log[-limit:])
        totals_snapshot = dict(_event_totals)
    return jsonify({
        'activity': activity_snapshot,
        'summary': {
            'sleeping': cached.get('success', 0),
            'pending': cached.get('pending', 0),
            'running': cached.get('running', 0),
        },
        'totals': totals_snapshot,
    })


def _parse_transfer_items(summary: str):
    """Parse transfer item summaries into Transfer Stacker item payloads."""
    if not summary:
        return [{'name': 'Item', 'qty': 1}]

    items = []
    for part in [p.strip() for p in summary.split(',') if p.strip()]:
        m = re.match(r'^(.*)\sx(\d+)$', part)
        if m:
            items.append({'name': (m.group(1) or part).strip(), 'qty': int(m.group(2) or 1)})
        else:
            items.append({'name': part, 'qty': 1})

    return items or [{'name': 'Item', 'qty': 1}]


def _parse_transfer_details(details: str, fallback_sender: str):
    """Convert freeform transfer_completed details into structured fields."""
    if not details:
        return {
            'name': 'Transfer',
            'sender': fallback_sender,
            'recipient': 'unknown',
            'items': [{'name': 'Item', 'qty': 1}],
        }

    parts = [p.strip() for p in details.split(' — ')]
    name = parts[0] if len(parts) > 0 and parts[0] else 'Transfer'
    flow = parts[1] if len(parts) > 1 else ''
    item_summary = parts[2] if len(parts) > 2 else 'Item x1'

    sender = fallback_sender
    recipient = 'unknown'
    if '→' in flow:
        sender_part, recipient_part = flow.split('→', 1)
        sender = (sender_part or fallback_sender).strip() or fallback_sender
        recipient = (recipient_part or 'unknown').strip() or 'unknown'

    return {
        'name': name,
        'sender': sender,
        'recipient': recipient,
        'items': _parse_transfer_items(item_summary),
    }


def _stable_int_id(value: str) -> int:
    """Generate deterministic numeric ids for UI list rendering keys."""
    digest = hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]
    return int(digest, 16)


@app.route('/api/simulation/transfer-stacker-log')
def simulation_transfer_stacker_log():
    """Return transfer_completed events in Transfer Stacker RemoteStack shape."""
    limit = request.args.get('limit', 200, type=int)
    limit = max(1, min(limit, MAX_LOG_ENTRIES))

    with _log_lock:
        transfer_events = [e for e in _activity_log if e.get('type') == 'transfer_completed']

    transfer_events = transfer_events[-limit:]

    now_iso = datetime.now(timezone.utc).isoformat()
    entries = []
    for ev in transfer_events:
        sender_fallback = ev.get('pod_full') or ev.get('pod') or 'unknown'
        parsed = _parse_transfer_details(ev.get('details'), sender_fallback)
        id_source = f"{ev.get('timestamp','')}|{ev.get('pod_full','')}|{ev.get('details','')}"
        entry = {
            'id': _stable_int_id(id_source),
            'name': parsed['name'],
            'createdByUserId': parsed['sender'],
            'createdByUsername': parsed['sender'],
            'createdAt': now_iso,
            'status': 'transferred',
            'recipient': parsed['recipient'],
            'transferredAt': now_iso,
            'items': parsed['items'],
        }
        entries.append(entry)

    return jsonify({
        'entries': entries,
        'source': 'smartprobe',
        'count': len(entries),
    })


@app.route('/api/simulation/agent-action', methods=['POST'])
def agent_action():
    """Receive real-time action reports from running agents."""
    data = request.get_json(force=True)
    pod = data.get('pod', 'unknown')
    action = data.get('action', '')
    details = data.get('details')

    # Map agent actions to activity event types
    ACTION_TYPES = {
        'browsing': 'browsing',
        'registered': 'registered',
        'logged_in': 'logged_in',
        'asset_created': 'asset_created',
        'asset_listed': 'asset_listed',
        'transfer_started': 'transfer_started',
        'transfer_completed': 'transfer_completed',
        'transfer_failed': 'transfer_failed',
        'conflict_detected': 'conflict_detected',
        'consistency_check': 'consistency_check',
        'agent_done': 'agent_done',
        # Basic probe events
        'probe_start': 'probe_start',
        'probe_get': 'probe_get',
        'probe_error': 'probe_error',
        'probe_done': 'probe_done',
    }
    event_type = ACTION_TYPES.get(action, 'action')
    add_activity_log(event_type, pod, details)

    # Persist latest state for this pod — used by the Agents tab (never trimmed)
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
    state = _agent_states.get(pod, {'username': None, 'history': []})
    if event_type in ('registered', 'logged_in') and details:
        state['username'] = details
    state.update({'timestamp': ts, 'type': event_type, 'details': details})
    # Rolling history of last 20 events per pod
    history = state.setdefault('history', [])
    history.append({'ts': ts, 'type': event_type, 'details': details})
    if len(history) > 20:
        del history[:len(history) - 20]
    _agent_states[pod] = state

    return jsonify({'status': 'ok'})


@app.route('/api/simulation/agent-states')
def get_agent_states():
    """Return persistent per-pod latest state (never trimmed)."""
    return jsonify({'states': _agent_states})


@app.route('/api/simulation/live-logs')
def live_logs():
    """Stream recent stdout from running agent pods via kubectl logs --prefix."""
    tail = request.args.get('tail', '8')
    try:
        tail = min(int(tail), 50)
    except (ValueError, TypeError):
        tail = 8

    # Find running pods
    list_result = run_cli_command([
        'kubectl', 'get', 'pods', '-l', 'app=playwright-agent',
        '--field-selector', 'status.phase=Running',
        '--no-headers', '-o', 'custom-columns=NAME:.metadata.name',
    ])
    pods = [p.strip() for p in (list_result.stdout or '').splitlines() if p.strip()][:10]

    if not pods:
        return jsonify({'lines': [], 'pods': 0})

    lines = []
    for pod in pods:
        result = run_cli_command(['kubectl', 'logs', pod, f'--tail={tail}'])
        if result.returncode == 0 and result.stdout:
            for line in result.stdout.splitlines():
                if line.strip():
                    lines.append({'pod': pod, 'line': line})

    return jsonify({'lines': lines, 'pods': len(pods)})


@app.route('/api/simulation/agent-result', methods=['POST'])
def agent_result():
    """Receive final JSON result from a completed agent."""
    data = request.get_json(force=True)
    _agent_results.append({
        'timestamp': datetime.now(timezone.utc).strftime('%H:%M:%S'),
        'pod': data.get('pod', 'unknown'),
        'persona': data.get('persona', 'unknown'),
        'status': data.get('status', 'unknown'),
        'actions': data.get('actions', []),
        'durationMs': data.get('durationMs', 0),
        'error': data.get('error'),
    })
    if len(_agent_results) > MAX_AGENT_RESULTS:
        del _agent_results[:len(_agent_results) - MAX_AGENT_RESULTS]
    return jsonify({'status': 'ok'})


@app.route('/api/simulation/agent-results')
def get_agent_results():
    """Return collected agent final results."""
    limit = request.args.get('limit', 100, type=int)
    limit = max(1, min(limit, MAX_AGENT_RESULTS))
    return jsonify({'results': _agent_results[-limit:]})


@app.route('/api/simulation/agent-detail/<pod_name>')
def get_agent_detail(pod_name):
    """Return latest state + result + coordinator role for a specific agent pod."""
    from simulation_service_tool.services.command_runner import is_valid_k8s_name
    if not is_valid_k8s_name(pod_name):
        return jsonify({'error': 'Invalid pod name'}), 400
    state = _agent_states.get(pod_name, {})
    result = next((r for r in reversed(_agent_results) if r.get('pod') == pod_name), None)

    # Fetch role from coordinator (best-effort)
    role = None
    try:
        import requests as _req
        r = _req.get(f'{COORDINATOR_URL}/agents', timeout=1)
        if r.ok:
            agents = r.json().get('agents', [])
            match = next((a for a in agents if a.get('pod') == pod_name), None)
            if match:
                role = match.get('role')
    except Exception:
        pass

    return jsonify({'pod': pod_name, 'state': state, 'result': result, 'role': role})


@app.route('/api/simulation/pod-logs/<pod_name>', methods=['GET'])
def get_pod_logs(pod_name):
    """Fetch stdout logs from a completed or running agent pod."""
    from simulation_service_tool.services.command_runner import is_valid_k8s_name
    if not is_valid_k8s_name(pod_name):
        return jsonify({'error': 'Invalid pod name'}), 400
    tail = request.args.get('tail', '300')
    try:
        tail = min(int(tail), 1000)
    except (ValueError, TypeError):
        tail = 300
    result = run_cli_command(["kubectl", "logs", pod_name, f"--tail={tail}"])
    if result.returncode != 0:
        return jsonify({'pod': pod_name, 'logs': result.stderr or 'No logs available', 'error': True})
    return jsonify({'pod': pod_name, 'logs': result.stdout or '(no output yet)'})


# ─── Coordinator proxy helpers ───────────────────────────────────────────────
COORDINATOR_URL = 'http://localhost:5003/api/coordinator'


def _coordinator_stats_safe():
    """Fetch coordinator stats from coordinator_service; returns empty dict on failure."""
    try:
        import requests as _req
        r = _req.get(f'{COORDINATOR_URL}/stats', timeout=1)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return {'agents': 0, 'roles': {}, 'pool_size': 0, 'transactions': {'total': 0, 'completed': 0, 'conflicts': 0, 'failed': 0}}


def _coordinator_reset_safe():
    """Tell coordinator_service to reset; silently ignored if not running."""
    try:
        import requests as _req
        _req.post(f'{COORDINATOR_URL}/reset', timeout=2)
    except Exception:
        pass


def _sync_active_test_to_service(payload: Optional[dict] = None) -> None:
    """Mirror active-test state into the running dashboard service from host CLI mode."""
    service_url = os.environ.get('SIMULATION_SERVICE_URL', 'http://localhost:5002').rstrip('/')
    headers = {'Authorization': f'Bearer {SIMULATION_API_KEY}'}
    try:
        import requests as _req
        if payload:
            _req.post(f'{service_url}/api/simulation/active-test', json=payload, headers=headers, timeout=2)
        else:
            _req.delete(f'{service_url}/api/simulation/active-test', headers=headers, timeout=2)
    except Exception:
        pass


@app.route('/api/simulation/active-test', methods=['POST', 'DELETE'])
@require_api_key
def sync_active_test():
    """Update run-scoped dashboard state for host-launched CLI runs."""
    if request.method == 'DELETE':
        _set_active_test()
        return jsonify({'status': 'ok'})

    payload = request.get_json(force=True) or {}
    _reset_run_state()
    return jsonify({'status': 'ok', 'active_test': _set_active_test(payload)})


@app.route('/api/simulation/coordinator/<path:subpath>', methods=['GET', 'POST'])
def coordinator_proxy(subpath):
    """Proxy coordinator API requests — keeps dashboard same-origin in local dev."""
    import requests as _req
    try:
        url = f'{COORDINATOR_URL}/{subpath}'
        if request.method == 'POST':
            r = _req.post(url, json=request.get_json(silent=True), timeout=2)
        else:
            r = _req.get(url, params=request.args, timeout=2)
        return Response(r.content, status=r.status_code, content_type=r.headers.get('Content-Type', 'application/json'))
    except Exception:
        return jsonify({'agents': [], 'count': 0}), 200


@app.route('/api/preflight', methods=['GET'])
def preflight_check_endpoint():
    """Check for conflicts before deployment"""
    try:
        result = K8sSimulationMonitor.preflight_check()
        return jsonify(result)
    except Exception as exc:
        logger.exception("preflight_check failed")
        return jsonify({'error': str(exc), 'has_conflicts': False, 'conflicts': []}), 500

@app.route('/api/simulation/start', methods=['POST'])
@require_api_key
def start_simulation():
    req = request.get_json(force=True)
    name = req.get('name', f"test-{int(time.time())}")
    completions = int(req.get('completions', 10))
    parallelism = int(req.get('parallelism', 5))
    persona = req.get('persona', 'impatient')
    workers = int(req.get('workers', 1))
    wait = bool(req.get('wait', False))
    skip_preflight = bool(req.get('skip_preflight', False))
    image_repository = (req.get('imageRepository') or '').strip() or None
    image_tag = (req.get('imageTag') or '').strip() or None
    replica_count = req.get('replicaCount')
    shard_total = req.get('shardTotal')
    request_memory = (req.get('requestMemory') or '').strip() or None
    request_cpu = (req.get('requestCpu') or '').strip() or None
    limit_memory = (req.get('limitMemory') or '').strip() or None
    limit_cpu = (req.get('limitCpu') or '').strip() or None
    backoff_limit = req.get('backoffLimit')
    ttl_seconds_after_finished = req.get('ttlSecondsAfterFinished')
    command_override = (req.get('commandOverride') or '').strip() or None
    kueue = req.get('kueue')
    mode = (req.get('mode') or '').strip() or 'basic'

    if mode not in SIMULATION_MODES:
        return jsonify({'success': False, 'error': f'Invalid mode. Must be one of: {SIMULATION_MODES}'}), 400

    # Reset coordinator state for the new run
    _coordinator_reset_safe()
    _reset_run_state()

    # Both basic and transactional modes use the custom playwright-agent image
    # which contains run.py. The raw mcr.microsoft.com/playwright image has no
    # CMD that runs any test script, so pods would complete instantly without
    # doing anything.
    if not image_repository:
        image_repository = 'playwright-agent'
    if not image_tag:
        image_tag = 'latest'
    if not command_override:
        command_override = 'python3 /app/run.py'
    # Keep jobs/pods alive long enough for the monitor to read stats.
    # 3600 s = 1 h; callers can still pass a shorter TTL explicitly.
    if ttl_seconds_after_finished is None:
        ttl_seconds_after_finished = 3600

    # Transactional mode: also set lighter resources (pure HTTP, no browser)
    if mode == 'transactional':
        # Coordinator-based role assignment and asset transfers
        pass  # image/command already set above

    # Basic mode: slightly more headroom than transactional (coordinator not required)
    if not request_memory:
        request_memory = '64Mi'
    if not request_cpu:
        request_cpu = '50m'
    if not limit_memory:
        limit_memory = '128Mi'
    if not limit_cpu:
        limit_cpu = '100m'

    replica_count = int(replica_count) if replica_count is not None else None
    shard_total = int(shard_total) if shard_total is not None else None
    backoff_limit = int(backoff_limit) if backoff_limit is not None else None
    ttl_seconds_after_finished = int(ttl_seconds_after_finished) if ttl_seconds_after_finished is not None else None

    if not is_valid_release_name(name):
        return jsonify({'success': False, 'error': 'Invalid release name. Use lowercase alphanumeric and hyphens only.'}), 400
    if not is_valid_persona(persona):
        return jsonify({'success': False, 'error': 'Invalid persona name.'}), 400
    if not (1 <= parallelism <= 200):
        return jsonify({'success': False, 'error': 'Parallelism must be between 1 and 200.'}), 400
    if not (1 <= completions <= 5000):
        return jsonify({'success': False, 'error': 'Completions must be between 1 and 5000.'}), 400
    if replica_count is not None and not (1 <= replica_count <= 5000):
        return jsonify({'success': False, 'error': 'Replica count must be between 1 and 5000.'}), 400
    if shard_total is not None and not (1 <= shard_total <= 5000):
        return jsonify({'success': False, 'error': 'Shard total must be between 1 and 5000.'}), 400
    if backoff_limit is not None and backoff_limit < 0:
        return jsonify({'success': False, 'error': 'Backoff limit must be zero or positive.'}), 400
    if ttl_seconds_after_finished is not None and ttl_seconds_after_finished < 0:
        return jsonify({'success': False, 'error': 'TTL seconds after finished must be zero or positive.'}), 400

    # Run preflight check unless explicitly skipped
    if not skip_preflight:
        preflight = K8sSimulationMonitor.preflight_check()
        if preflight['has_conflicts']:
            return jsonify({
                'success': False,
                'error': 'Resource conflicts detected',
                'conflicts': preflight['conflicts'],
                'suggestion': 'Run cleanup first or use /api/cleanup/stuck, or pass skip_preflight=true to override'
            }), 409

    _test_t0 = time.time()
    result = TestController.run_test(
        name,
        completions,
        parallelism,
        persona,
        workers,
        wait,
        image_repository=image_repository,
        image_tag=image_tag,
        replica_count=replica_count,
        shard_total=shard_total,
        request_memory=request_memory,
        request_cpu=request_cpu,
        limit_memory=limit_memory,
        limit_cpu=limit_cpu,
        backoff_limit=backoff_limit,
        ttl_seconds_after_finished=ttl_seconds_after_finished,
        command_override=command_override,
        kueue=kueue,
        probe_mode=mode,
        probe_url=(req.get('probeUrl') or '').strip() or None,
    )
    _test_duration = time.time() - _test_t0
    action_result = 'success' if result.get('success') else 'failure'
    AGENT_ORCHESTRATION_TOTAL.labels(action='start_test', result=action_result).inc()
    # Derive preset label from the test name prefix (e.g. "medium-1234" → "medium")
    _preset = name.rsplit('-', 1)[0] if '-' in name else 'custom'
    if _preset not in PRESETS:
        _preset = 'custom'
    AGENT_TEST_DURATION.labels(preset=_preset, persona=persona).observe(_test_duration)
    return jsonify(result)

@app.route('/api/simulation/stop', methods=['POST'])
@require_api_key
def stop_simulation():
    req = request.get_json(force=True)
    name = req.get('name')
    if not name:
        return jsonify({'success': False, 'error': 'Missing test name'}), 400
    if not is_valid_release_name(name):
        return jsonify({'success': False, 'error': 'Invalid release name. Use lowercase alphanumeric and hyphens only.'}), 400
    result = TestController.stop_test(name)
    action_result = 'success' if result.get('success') else 'failure'
    AGENT_ORCHESTRATION_TOTAL.labels(action='stop_test', result=action_result).inc()
    return jsonify(result)

@app.route('/api/simulation/tests')
def list_tests():
    return jsonify(TestController.list_tests())

@app.route('/api/simulation/presets')
def list_presets():
    return jsonify(PRESETS)

@app.route('/api/simulation/status')
def simulation_status():
    return jsonify(get_status())

# CLI interface

def main():
    parser = argparse.ArgumentParser(description="Simulation Service CLI")
    subparsers = parser.add_subparsers(dest='command')

    # Start
    start_parser = subparsers.add_parser('start', help='Start a test')
    start_parser.add_argument('--name', type=str, default=None)
    start_parser.add_argument('--completions', type=int, default=None)
    start_parser.add_argument('--parallelism', type=int, default=None)
    start_parser.add_argument('--persona', type=str, default=None)
    start_parser.add_argument('--workers', type=int, default=None)
    start_parser.add_argument('--wait', action='store_true')
    start_parser.add_argument('--preset', type=str, choices=PRESETS.keys(), help='Test preset (overrides other options)')
    start_parser.add_argument('--mode', type=str, choices=SIMULATION_MODES, default='basic', help='Simulation mode: basic (e2e test) or transactional (browse+transfer agent)')

    # Stop
    stop_parser = subparsers.add_parser('stop', help='Stop a test')
    stop_parser.add_argument('name', type=str)

    # List
    subparsers.add_parser('list', help='List running tests')

    # Status
    subparsers.add_parser('status', help='Show simulation summary')
    watch_parser = subparsers.add_parser('watch', help='Watch simulation summary')
    watch_parser.add_argument('--name', type=str, default=None, help='Specific release to watch')

    # Presets
    subparsers.add_parser('presets', help='List test presets')

    # Server
    server_parser = subparsers.add_parser('server', help='Run Flask server')
    server_parser.add_argument('--host', type=str, default='0.0.0.0')
    server_parser.add_argument('--port', type=int, default=5002)
    server_parser.add_argument('--debug', action='store_true')

    args = parser.parse_args()

    if args.command == 'start':
        if args.preset:
            preset = PRESETS[args.preset]
            completions = preset['completions']
            parallelism = preset['parallelism']
            persona = preset['persona']
            workers = preset.get('workers', 1)
            mode = preset.get('mode', 'basic')
        else:
            completions = args.completions if args.completions is not None else 10
            parallelism = args.parallelism if args.parallelism is not None else 5
            persona = args.persona if args.persona is not None else 'impatient'
            workers = args.workers if args.workers is not None else 1
            mode = args.mode
        # Apply mode overrides
        command_override = None
        image_repository = None
        image_tag = None
        if mode == 'transactional':
            image_repository = 'playwright-agent'
            image_tag = 'latest'
            command_override = 'python3 /app/run.py'
        name = args.name or f"test-{int(time.time())}"
        result = TestController.run_test(
            name,
            completions,
            parallelism,
            persona,
            workers,
            args.wait,
            image_repository=image_repository,
            image_tag=image_tag,
            command_override=command_override,
        )
        print(json.dumps(result, indent=2))
        if result.get('success'):
            prompt_start_success_next_steps(result)
        else:
            prompt_start_failure_recovery(result.get('error', ''))
    elif args.command == 'stop':
        result = TestController.stop_test(args.name)
        print(json.dumps(result, indent=2))
    elif args.command == 'list':
        tests = TestController.list_tests()
        print(json.dumps(tests, indent=2))
    elif args.command == 'status':
        summary = get_status()
        print(json.dumps(summary, indent=2))
    elif args.command == 'watch':
        watch_release_progress(args.name)
    elif args.command == 'presets':
        print(json.dumps(PRESETS, indent=2))
    elif args.command == 'server':
        app.run(host=args.host, port=args.port, debug=args.debug)
    else:
        parser.print_help()

def interactive_menu():
    """Interactive menu when no command is provided"""
    import questionary
    while True:
        action = questionary.select(
            "What would you like to do?",
            choices=[
                "Show Status",
                "Start a Test",
                "Stop a Test",
                "List Tests",
                "Watch Progress",
                "Show Presets",
                "Start Server",
                "Exit"
            ]
        ).ask()
        if action == "Exit":
            print("Goodbye!")
            break
        elif action == "Show Status":
            os.system("python3 simulation_service.py status")
        elif action == "Start a Test":
            preset = choose_preset_menu_detailed()
            if preset == "back":
                continue
            elif preset == "custom":
                # Prompt for custom options
                completions = questionary.text("Number of agents (completions):", default="10").ask()
                parallelism = questionary.text("Parallelism:", default="5").ask()
                persona = questionary.select(
                    "Persona:",
                    choices=["impatient", "strategic", "browser"]
                ).ask()
                name = questionary.text("Test name (optional):").ask()
                cmd = f"python3 simulation_service.py start --completions {completions} --parallelism {parallelism} --persona {persona}"
                if name:
                    cmd += f" --name {name}"
                os.system(cmd)
            else:
                name = questionary.text("Test name (optional):").ask()
                if name:
                    os.system(f"python3 simulation_service.py start --preset {preset} --name {name}")
                else:
                    os.system(f"python3 simulation_service.py start --preset {preset}")
        elif action == "Stop a Test":
            os.system("python3 simulation_service.py list")
            name = questionary.text("Enter test name to stop:").ask()
            if name:
                os.system(f"python3 simulation_service.py stop {name}")
        elif action == "List Tests":
            os.system("python3 simulation_service.py list")
        elif action == "Watch Progress":
            os.system("python3 simulation_service.py watch")
        elif action == "Show Presets":
            os.system("python3 simulation_service.py presets")
        elif action == "Start Server":
            os.system("python3 simulation_service.py server")
        input("\nPress Enter to continue...")
# Standalone version of choose_preset_menu_detailed for interactive_menu
def choose_preset_menu_detailed():
    preset = questionary.select(
        "Choose preset:",
        choices=[
            questionary.Choice(
                title="tiny   │ 5 agents │ 2 parallel │ ~10s │ Quick sanity check",
                value="tiny"
            ),
            questionary.Choice(
                title="small  │ 10 agents │ 5 parallel │ ~30s │ Dev testing",
                value="small"
            ),
            questionary.Choice(
                title="medium │ 50 agents │ 10 parallel │ ~2m │ Integration testing",
                value="medium"
            ),
            questionary.Choice(
                title="large  │ 100 agents │ 20 parallel │ ~5m │ Performance testing",
                value="large"
            ),
            questionary.Choice(
                title="xlarge │ 500 agents │ 50 parallel │ ~15m │ Stress testing",
                value="xlarge"
            ),
            questionary.Separator(),
            questionary.Choice(
                title="CUSTOM - Set your own values",
                value="custom"
            ),
            questionary.Separator(),
            questionary.Choice(
                title="Back",
                value="back"
            ),
        ]
    ).ask()
    return preset

# Wrap the existing CLI logic for reuse

def cli():
    main()

if __name__ == '__main__':
    import sys
    # If no arguments, show interactive menu
    if len(sys.argv) == 1:
        try:
            if os.environ.get('SIMULATION_SERVICE_LEGACY_MENU') == '1':
                import questionary
                interactive_menu()
            else:
                print("Launching advanced CLI from simulation_service_tool.py")
                os.execv(sys.executable, [sys.executable, "simulation_service_tool.py"])
        except ImportError:
            print("🎮 Agent Control CLI")
            print("=" * 40)
            print("Commands: start, stop, list, status, watch, presets, server")
            print("\nExample: python3 simulation_service.py status")
            print("\nInstall questionary for interactive menu: pip3 install questionary")
        except OSError as exc:
            print(f"Failed to launch advanced CLI: {exc}")
            print("Falling back to legacy interactive menu.")
            interactive_menu()
    else:
        cli()

