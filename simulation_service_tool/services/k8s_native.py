"""Native Kubernetes Python client initialization and monitoring.

Provides K8s API client setup with proper socket/request timeouts,
and a K8sNativeMonitor for querying pods, jobs, and building dashboard
summaries.  Extracted from simulation_service.py so the timeout and
connectivity logic lives in one place.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kubernetes SDK availability
# ---------------------------------------------------------------------------

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False
    client = None  # type: ignore[assignment]
    config = None  # type: ignore[assignment]
    ApiException = Exception  # type: ignore[misc,assignment]

# ---------------------------------------------------------------------------
# Timeout configuration (seconds)
# ---------------------------------------------------------------------------

# Socket-level connect / read timeouts applied to the urllib3 pool used
# by the kubernetes-client SDK.  Without these the default is *no timeout*,
# meaning a single ``list_namespaced_pod`` call can hang forever when the
# API server is unreachable (e.g. Docker Desktop K8s stopped).
K8S_CONNECT_TIMEOUT = 3
K8S_READ_TIMEOUT = 5

# Per-request timeout tuple passed to every SDK call via ``_request_timeout``.
# Format: (connect_timeout, read_timeout)
REQUEST_TIMEOUT: tuple[int, int] = (K8S_CONNECT_TIMEOUT, K8S_READ_TIMEOUT)

# ---------------------------------------------------------------------------
# Host validation
# ---------------------------------------------------------------------------

_UNUSABLE_K8S_HOSTS = {
    '',
    'http://localhost',
    'https://localhost',
    'http://localhost:80',
    'https://localhost:80',
    'http://127.0.0.1',
    'https://127.0.0.1',
    'http://127.0.0.1:80',
    'https://127.0.0.1:80',
}


def _is_unusable_k8s_host(host: str) -> bool:
    normalized = (host or '').strip().lower().rstrip('/')
    return normalized in _UNUSABLE_K8S_HOSTS


# ---------------------------------------------------------------------------
# Client initialization
# ---------------------------------------------------------------------------

def initialize_native_k8s_clients():
    """Create and return (CoreV1Api, BatchV1Api, PolicyV1Api, error_reason).

    On success the fourth element is ``None``; on failure the first three
    are ``None`` and the fourth explains why.

    Socket-level timeouts are injected into the shared :class:`Configuration`
    so that *every* subsequent HTTP call is bounded.
    """
    if not K8S_AVAILABLE:
        return None, None, None, 'kubernetes Python client is not installed'

    try:
        kubeconfig_path = os.path.expanduser('~/.kube/config')
        if os.path.exists(kubeconfig_path):
            config.load_kube_config(config_file=kubeconfig_path)
            logger.info(f"Loaded kubeconfig from {kubeconfig_path}")
        else:
            config.load_incluster_config()
            logger.info("Loaded in-cluster kubeconfig")
    except Exception as exc:
        reason = f"could not load Kubernetes config: {exc}"
        logger.warning(f"Native Kubernetes client disabled: {reason}")
        return None, None, None, reason

    try:
        configuration = client.Configuration.get_default_copy()
        host = (configuration.host or '').strip()
        if _is_unusable_k8s_host(host):
            reason = f"unusable Kubernetes API host '{host or 'unset'}'"
            logger.warning(f"Native Kubernetes client disabled: {reason}")
            return None, None, None, reason

        api_client = client.ApiClient(configuration=configuration)
        logger.info(f"Native Kubernetes client configured for {host}")
        return (
            client.CoreV1Api(api_client),
            client.BatchV1Api(api_client),
            client.PolicyV1Api(api_client),
            None,
        )
    except Exception as exc:
        reason = f"could not initialize Kubernetes API clients: {exc}"
        logger.warning(f"Native Kubernetes client disabled: {reason}")
        return None, None, None, reason


# Module-level singleton clients
v1, batch_v1, policy_v1, K8S_CLIENT_DISABLED_REASON = initialize_native_k8s_clients()


def native_k8s_client_enabled() -> bool:
    return v1 is not None and batch_v1 is not None


# ---------------------------------------------------------------------------
# K8sNativeMonitor
# ---------------------------------------------------------------------------

class K8sNativeMonitor:
    """Monitors Kubernetes using native Python client."""

    def __init__(self, namespace: str = "default"):
        self.namespace = namespace

    def get_pods(self, label_selector: str = "app=playwright-agent") -> list:
        if v1 is None:
            return []
        try:
            pod_list = v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=label_selector,
                _request_timeout=REQUEST_TIMEOUT,
            )
            return pod_list.items
        except Exception as e:
            logger.error(f"K8s API error: {e}")
            return []

    def get_pod_logs(self, pod_name: str, tail_lines: int = 50) -> str:
        if v1 is None:
            return ""
        try:
            logs = v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=self.namespace,
                tail_lines=tail_lines,
                _request_timeout=REQUEST_TIMEOUT,
            )
            return logs
        except ApiException as e:
            if e.status == 404:
                logger.debug(f"Pod {pod_name} not found (already cleaned up)")
            else:
                logger.warning(f"Failed to get logs for {pod_name}: {e.status} {e.reason}")
            return ""
        except Exception as e:
            logger.warning(f"Failed to get logs for {pod_name}: {e}")
            return ""

    def get_jobs(self, label_selector: str = "app=playwright-agent") -> list:
        if batch_v1 is None:
            return []
        try:
            job_list = batch_v1.list_namespaced_job(
                namespace=self.namespace,
                label_selector=label_selector,
                _request_timeout=REQUEST_TIMEOUT,
            )
            return job_list.items
        except Exception as e:
            logger.error(f"K8s API error getting jobs: {e}")
            return []

    def get_detailed_summary(self) -> Dict[str, Any]:
        pods = self.get_pods()
        jobs = self.get_jobs()

        pod_total = len(pods)
        succeeded = 0
        running = 0
        failed = 0
        pending = 0

        results: List[Dict[str, Any]] = []
        personas: Dict[str, int] = {}
        job_stats: Dict[str, Dict[str, Any]] = {}

        for job in jobs:
            job_name = job.metadata.name
            job_status = job.status
            job_stats[job_name] = {
                'active': job_status.active or 0,
                'succeeded': job_status.succeeded or 0,
                'failed': job_status.failed or 0,
                'completions': job.spec.completions,
                'parallelism': job.spec.parallelism,
            }

        total = sum(stats['completions'] or 0 for stats in job_stats.values()) or pod_total

        for pod in pods[:50]:
            pod_name = pod.metadata.name
            phase = pod.status.phase
            start_time = pod.status.start_time
            created_at = pod.metadata.creation_timestamp

            labels = pod.metadata.labels or {}
            persona = labels.get('persona', 'unknown')

            annotations = pod.metadata.annotations or {}
            target = annotations.get('target', 'https://example.com')

            if phase == 'Succeeded':
                succeeded += 1
                status = 'completed'
            elif phase == 'Running':
                running += 1
                status = 'running'
            elif phase == 'Failed':
                failed += 1
                status = 'error'
            else:
                pending += 1
                status = 'pending'

            personas[persona] = personas.get(persona, 0) + 1

            actions: List[Dict[str, str]] = []
            errors: List[str] = []
            if phase in ['Succeeded', 'Failed'] and len(results) < 20:
                logs = self.get_pod_logs(pod_name, tail_lines=100)
                actions = self.parse_actions_from_logs(logs)
                errors = self.parse_errors_from_logs(logs)

                # Activation latency: time from creation to pod start
                activation_secs = None
                if created_at and start_time:
                    activation_secs = round((start_time - created_at).total_seconds(), 1)

                results.append({
                    'pod': pod_name,
                    'persona': persona,
                    'status': status,
                    'phase': phase,
                    'target': target,
                    'createdAt': created_at.isoformat() if created_at else None,
                    'startTime': start_time.isoformat() if start_time else None,
                    'activationLatency': activation_secs,
                    'actions': actions,
                    'errors': errors[:5],
                    'error': errors[0] if errors else None,
                })

        results.sort(key=lambda x: x.get('startTime', ''), reverse=True)

        # Compute activation latency stats across ALL pods
        latencies: List[float] = []
        for pod in pods[:50]:
            ct = pod.metadata.creation_timestamp
            st = pod.status.start_time
            if ct and st:
                latencies.append(round((st - ct).total_seconds(), 1))

        activation_stats: Dict[str, Any] = {}
        if latencies:
            latencies_sorted = sorted(latencies)
            n = len(latencies_sorted)
            activation_stats = {
                'avg': round(sum(latencies_sorted) / n, 1),
                'min': latencies_sorted[0],
                'max': latencies_sorted[-1],
                'p50': latencies_sorted[n // 2],
                'p95': latencies_sorted[min(int(n * 0.95), n - 1)],
                'count': n,
            }

        # Compute throughput: agents/sec, avg duration, ETA
        throughput: Dict[str, Any] = {}
        now = datetime.now(timezone.utc)
        creation_times = []
        durations: List[float] = []
        for pod in pods[:50]:
            ct = pod.metadata.creation_timestamp
            if ct:
                creation_times.append(ct)
            # Extract actual execution duration from container terminated state
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    term = cs.state.terminated if cs.state else None
                    if term and term.started_at and term.finished_at:
                        dur = (term.finished_at - term.started_at).total_seconds()
                        if dur >= 0:
                            durations.append(round(dur, 1))

        completed = succeeded + failed
        if creation_times:
            earliest = min(creation_times)
            elapsed = (now - earliest).total_seconds()
            if elapsed > 0 and completed > 0:
                rate = completed / elapsed
                remaining = total - completed
                eta = remaining / rate if rate > 0 else 0
                throughput = {
                    'agentsPerSecond': round(rate, 2),
                    'agentsPerMinute': round(rate * 60, 1),
                    'elapsed': round(elapsed, 1),
                    'completed': completed,
                    'percentComplete': round((completed / total) * 100, 1) if total > 0 else 0,
                    'etaSeconds': round(eta, 1),
                }
        if durations:
            durations_sorted = sorted(durations)
            n_dur = len(durations_sorted)
            throughput['avgDuration'] = round(sum(durations_sorted) / n_dur, 1)
            throughput['minDuration'] = durations_sorted[0]
            throughput['maxDuration'] = durations_sorted[-1]

        return {
            'total': total,
            'activePods': pod_total,
            'success': succeeded,
            'running': running,
            'errors': failed,
            'pending': pending,
            'personas': personas,
            'jobs': job_stats,
            'results': results[:20],
            'activation': activation_stats,
            'throughput': throughput,
            'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        }

    @staticmethod
    def parse_actions_from_logs(logs: str) -> List[Dict]:
        actions = []
        patterns = [
            (r'click(?:ed)?\s+["\']?([^"\'\n]+)', 'click'),
            (r'navigat(?:ed|ing)?\s+to\s+([^\s\n]+)', 'navigate'),
            (r'fill(?:ed)?\s+["\']?([^"\'\n]+)', 'fill'),
            (r'wait(?:ed)?\s+(\d+)ms', 'wait'),
            (r'screenshot\s+["\']?([^"\'\n]+)', 'screenshot'),
        ]

        for line in logs.split('\n'):
            for pattern, action_type in patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    actions.append({
                        'type': action_type,
                        'target': match.group(1).strip(),
                        'raw': line.strip()[:100],
                    })

        return actions[-20:]

    @staticmethod
    def parse_errors_from_logs(logs: str) -> List[str]:
        errors = []
        error_patterns = [
            r'Error:?\s+([^\n]+)',
            r'Exception:?\s+([^\n]+)',
            r'FAILED?\s+([^\n]+)',
            r'TimeoutError:?\s+([^\n]+)',
        ]

        for line in logs.split('\n'):
            for pattern in error_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    errors.append(line.strip()[:200])
                    break

        return errors[-10:]
