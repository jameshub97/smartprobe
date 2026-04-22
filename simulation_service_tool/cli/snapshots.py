"""Cluster state snapshot builders for menu rendering."""

from datetime import datetime, timezone
import json

from simulation_service_tool.services.command_runner import run_cli_command
from simulation_service_tool.services.direct_cleanup import direct_preflight_check
from simulation_service_tool.menus.ports import get_port_status


def _kubectl_get_json(resource_type, resource_name):
    result = run_cli_command(["kubectl", "get", resource_type, resource_name, "-o", "json"])
    if result.returncode != 0 or not result.stdout.strip():
        return None, result.stderr.strip() or f"{resource_type}/{resource_name} not found"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"Invalid kubectl JSON for {resource_type}/{resource_name}: {exc}"


def _kubectl_list_json(resource_type, label_selector=None):
    args = ["kubectl", "get", resource_type, "-o", "json"]
    if label_selector:
        args.extend(["-l", label_selector])

    result = run_cli_command(args)
    if result.returncode != 0 or not result.stdout.strip():
        return [], result.stderr.strip() or f"{resource_type} not found"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return [], f"Invalid kubectl JSON for {resource_type}: {exc}"
    return payload.get('items', []), None


def _extract_waiting_reason(pod):
    container_statuses = ((pod or {}).get('status', {}) or {}).get('containerStatuses', []) or []
    for container_status in container_statuses:
        waiting = (container_status.get('state', {}) or {}).get('waiting') or {}
        reason = waiting.get('reason')
        if reason:
            return reason
    return None


def _collect_release_pod_assessment():
    pods, error = _kubectl_list_json('pods', 'app=playwright-agent')
    if error:
        return {
            'total': 0,
            'healthy': 0,
            'waiting_reasons': [],
            'error': error,
        }

    healthy = 0
    waiting_reasons = []

    for pod in pods:
        status = pod.get('status', {}) or {}
        container_statuses = status.get('containerStatuses', []) or []
        pod_ready = any(
            condition.get('type') == 'Ready' and condition.get('status') == 'True'
            for condition in (status.get('conditions', []) or [])
        )
        if pod_ready:
            healthy += 1

        for container_status in container_statuses:
            state = container_status.get('state', {}) or {}
            waiting = state.get('waiting') or {}
            reason = waiting.get('reason')
            if reason:
                waiting_reasons.append(reason)

    return {
        'total': len(pods),
        'healthy': healthy,
        'waiting_reasons': sorted(set(waiting_reasons)),
        'error': None,
    }


def _get_statefulset_stale_status(pod_name='playwright-agent-0', statefulset_name='playwright-agent'):
    pod, pod_error = _kubectl_get_json('pod', pod_name)
    if pod_error:
        return None

    statefulset, statefulset_error = _kubectl_get_json('statefulset', statefulset_name)
    if statefulset_error:
        return None

    pod_labels = (pod.get('metadata', {}) or {}).get('labels', {}) or {}
    statefulset_status = statefulset.get('status', {}) or {}
    pod_revision = pod_labels.get('controller-revision-hash') or 'unknown'
    current_revision = statefulset_status.get('updateRevision') or statefulset_status.get('currentRevision') or 'unknown'
    waiting_reason = _extract_waiting_reason(pod)

    return {
        'pod_name': pod_name,
        'pod_revision': pod_revision,
        'current_revision': current_revision,
        'pod_created': (pod.get('metadata', {}) or {}).get('creationTimestamp'),
        'waiting_reason': waiting_reason,
        'is_stale': bool(pod_revision and current_revision and pod_revision != current_revision),
        'is_crashing': waiting_reason == 'CrashLoopBackOff' or (pod.get('status', {}) or {}).get('phase') == 'Error',
    }


def _format_pod_age(creation_timestamp):
    if not creation_timestamp:
        return 'unknown'
    try:
        created = datetime.fromisoformat(creation_timestamp.replace('Z', '+00:00'))
    except ValueError:
        return creation_timestamp

    delta = datetime.now(timezone.utc) - created
    total_seconds = max(int(delta.total_seconds()), 0)
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m"
    if total_seconds < 86400:
        return f"{total_seconds // 3600}h"
    return f"{total_seconds // 86400}d"


def _pod_ready_value(pod):
    container_statuses = ((pod.get('status', {}) or {}).get('containerStatuses', []) or [])
    if not container_statuses:
        return '0/0'
    ready_count = sum(1 for status in container_statuses if status.get('ready'))
    return f"{ready_count}/{len(container_statuses)}"


def _pod_restart_count(pod):
    container_statuses = ((pod.get('status', {}) or {}).get('containerStatuses', []) or [])
    return sum(status.get('restartCount', 0) for status in container_statuses)


def _pod_status_value(pod):
    waiting_reason = _extract_waiting_reason(pod)
    if waiting_reason:
        return waiting_reason
    return ((pod.get('status', {}) or {}).get('phase') or 'Unknown')


def get_welcome_snapshot(
    service_running,
    progress_callback=None,
    include_preflight=False,
    include_stale=False,
    include_pods=False,
):
    progress = progress_callback or (lambda _message: None)

    if include_pods:
        progress("Checking Kubernetes pod status...")
        pod_assessment = _collect_release_pod_assessment()
    else:
        progress("Skipping Kubernetes pod scan for fast startup...")
        pod_assessment = {
            'total': 0,
            'healthy': 0,
            'waiting_reasons': [],
            'error': None,
        }

    pod_error = pod_assessment.get('error')
    if pod_error:
        progress(f"Pod check warning: {pod_error}")

    if include_stale:
        progress("Inspecting stateful set health...")
        stale_info = _get_statefulset_stale_status()
    else:
        stale_info = None

    progress("Scanning local development ports...")
    port_statuses = get_port_status()
    active_ports = sum(1 for status in port_statuses.values() if status.get('in_use'))

    if include_preflight:
        progress("Running cluster preflight checks...")
        preflight = direct_preflight_check()
    else:
        preflight = {'conflicts': []}
    conflicts = preflight.get('conflicts', [])

    cluster_summary = {
        'service_running': service_running,
        'active_pods': pod_assessment.get('total', 0),
        'healthy_pods': pod_assessment.get('healthy', 0),
        'unhealthy_pods': max(pod_assessment.get('total', 0) - pod_assessment.get('healthy', 0), 0),
        'waiting_reasons': pod_assessment.get('waiting_reasons', []),
        'pods_pending': not include_pods,
        'active_ports': active_ports,
        'stale_pod': stale_info,
        'stale_pending': not include_stale,
        'orphaned_conflicts': conflicts,
        'orphaned_count': len(conflicts),
        'preflight_pending': not include_preflight,
    }

    progress("Cluster snapshot ready.")

    return cluster_summary


def get_routine_checks_snapshot(service_running, include_preflight=False, include_stale=False, include_pods=False):
    pods, pod_error = _kubectl_list_json('pods', 'app=playwright-agent') if include_pods else ([], None)
    preflight = direct_preflight_check() if include_preflight else {'conflicts': []}
    port_statuses = get_port_status()
    active_ports = [
        {
            'port': port,
            'service': status.get('service', 'Unknown'),
            'summary': status.get('processes', [{}])[0].get('command', 'in use') if status.get('in_use') else 'free',
        }
        for port, status in port_statuses.items()
        if status.get('in_use')
    ]

    pod_rows = []
    unhealthy_pods = []
    for pod in pods:
        row = {
            'name': (pod.get('metadata', {}) or {}).get('name', 'unknown'),
            'ready': _pod_ready_value(pod),
            'status': _pod_status_value(pod),
            'restarts': _pod_restart_count(pod),
            'age': _format_pod_age((pod.get('metadata', {}) or {}).get('creationTimestamp')),
        }
        pod_rows.append(row)
        if row['ready'] != '1/1' or row['status'] not in {'Running', 'Succeeded'}:
            unhealthy_pods.append(row)

    return {
        'service_running': service_running,
        'pod_error': pod_error,
        'pods': pod_rows,
        'unhealthy_pods': unhealthy_pods,
        'pods_pending': not include_pods,
        'active_ports': active_ports,
        'preflight_conflicts': preflight.get('conflicts', []),
        'preflight_pending': not include_preflight,
        'stale_pod': _get_statefulset_stale_status() if include_stale else None,
        'stale_pending': not include_stale,
    }
