"""Support helpers for preflight orchestration.

Keep pure helpers and lightweight runtime probes here so ``cli/preflight.py``
can stay focused on user-facing orchestration.
"""

import re
import socket


def extract_conflicting_release(error_message):
    match = re.search(r'current value is "([^"]+)"', error_message or "")
    return match.group(1) if match else None


def should_fallback_to_direct(error_message):
    message = (error_message or '').lower()
    fallback_markers = [
        '404',
        'not found',
        '<!doctype html',
        '<html',
        'could not connect to simulation service',
    ]
    return any(marker in message for marker in fallback_markers)


def fallback_info_lines(message, error_message, endpoint_label):
    endpoint = endpoint_label or 'service operation'
    lines = [
        '[36m[INFO][0m Fallback Details',
        '========================================',
        '',
        'Why this happened:',
        f"- The CLI requested: {endpoint}",
        '- The simulation service is reachable, but this endpoint was unavailable from the running process.',
        '- In practice this usually means the service was started from older code and needs a restart to load newer routes.',
    ]

    if error_message:
        details = error_message.strip()
        if len(details) > 500:
            details = details[:500].rstrip() + '...'
        lines.append(f"- Service response: {details}")

    lines.extend([
        '',
        'What direct mode means:',
        '- The CLI will run the same cleanup logic locally with kubectl and helm instead of calling the Flask API.',
    ])

    lower_endpoint = endpoint.lower()
    if '/api/cleanup/stuck' in lower_endpoint:
        lines.extend([
            '- Likely commands:',
            '  - kubectl delete pvc playwright-cache --ignore-not-found',
            '  - kubectl delete pdb playwright-agent-pdb --ignore-not-found',
            '  - kubectl delete resources labeled app=playwright-agent when they are stuck',
        ])
    elif '/api/cleanup/release/' in lower_endpoint:
        release_name = endpoint.rsplit('/', 1)[-1]
        lines.extend([
            '- Likely commands:',
            f'  - helm uninstall {release_name} --ignore-not-found',
            f'  - kubectl delete pods,pvc,pdb,jobs -l release={release_name} --ignore-not-found',
        ])
    elif '/api/cleanup/all' in lower_endpoint or '/api/cleanup/reset' in lower_endpoint:
        lines.extend([
            '- Likely commands:',
            '  - helm uninstall <detected-test-release> --ignore-not-found',
            '  - kubectl delete pods,pvc,pdb,jobs,statefulset -l app=playwright-agent --ignore-not-found',
        ])
    elif '/api/preflight' in lower_endpoint or '/api/cleanup/preflight' in lower_endpoint:
        lines.extend([
            '- Direct mode is read-only here: it inspects Helm releases, PVCs, and PDBs with local kubectl/helm commands.',
        ])

    lines.extend([
        '',
        'Is this safe?',
        '- Yes. The cleanup target stays the same; only the execution path changes.',
        '- The CLI still keeps you in the same flow and you can return to the conflict menu without losing context.',
        '',
        'What should you do?',
        '- Continue with direct cleanup if you want the fastest path to a clean cluster.',
        '- Return to the conflict menu if you want to inspect first.',
        '',
        'How to fix this permanently:',
        '- Restart the simulation service so it loads the latest routes from the current codebase.',
        '- If the problem persists after restart, then the running service entrypoint is not using this updated module.',
    ])
    return lines


def probe_sim_api(timeout: float = 2.5) -> bool:
    """Quick API responsiveness probe for the simulation service."""
    try:
        import requests
        from simulation_service_tool.ui.styles import SERVICE_URL

        requests.get(f"{SERVICE_URL}/health", timeout=timeout)
        return True
    except Exception:
        return False


def check_docker_services() -> dict:
    """Check local test dependencies are actually responding.

    Uses HTTP health endpoints (port-bound != service-ready).
    Falls back to TCP if the HTTP probe raises an unexpected error.
    """

    def _http_health(url: str, timeout: float = 2.5) -> bool:
        try:
            import requests
            resp = requests.get(url, timeout=timeout)
            return resp.status_code == 200
        except Exception:
            return False

    return {
        'simulation': _http_health('http://localhost:5002/health'),
        'backend': _http_health('http://localhost:5001/health'),
    }


def clear_hung_api_listeners_before_preflight(service_running: bool, *, cleanup_fn, print_fn=print) -> bool:
    """Clear hung local API listeners before test preflight starts."""
    cleanup = cleanup_fn(restart_simulation=False)

    if cleanup.get('released_ports'):
        print_fn(f"[36m[INFO][0m Cleared hung API listeners before preflight: {cleanup['detail']}")
    if cleanup.get('failures'):
        print_fn(f"[33m[WARN][0m Hung API cleanup before preflight was incomplete: {cleanup['detail']}")

    if '5002' in cleanup.get('released_ports', []):
        print_fn("[36m[INFO][0m Simulation service listener was reset. Falling back to direct preflight checks.")
        return False

    return service_running