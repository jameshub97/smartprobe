"""Smart diagnostics — detects baseline drift and provides actionable remediation.

Two-pillar architecture
-----------------------
  Pillar 1 — Initialization  (services/cluster_init.py + cli/commands.initialize_cluster_menu)
    Sets a known-good baseline once: installs Helm charts, creates namespaces,
    applies RBAC, etc.  ``is_initialized()`` returns True from that point on.

  Pillar 2 — Smart Diagnostics  (this module)
    Called every time ``welcome_menu()`` loops (only when cluster_initialized=True).
    Detects drift from the baseline and surfaces actionable, numbered fixes.

Drift-check pipeline  (run_drift_checks)
-----------------------------------------
Checks run sequentially and short-circuit on hard blockers:

  1. Docker daemon reachable?     → _docker_running()
       No  → finding(docker_not_running),  skip remaining checks
  2. Kubernetes API reachable?    → k8s_connectivity.k8s_reachable()
       No  → finding(k8s_unreachable, severity=warning, NO action),  skip k8s checks
       Note: k8s_unreachable has NO action key on purpose — auto-recovery
             via minikube/context-scan is intentionally removed from "Fix All".
             Users must go to Diagnostics → K8s Connectivity.
  3. Cluster resource drift       → direct_verify_state()
       orphaned Helm releases     → finding(orphaned_releases,  action=clean_orphans)
       orphaned PVCs              → finding(orphaned_pvcs,      action=clean_orphans)
       conflicting PDBs           → finding(conflicting_pdbs,   action=clean_orphans)
  4. Pod drift
       stale non-Running pods     → finding(residual_pods,      action=clean_orphans)
       Running but not-ready pods → finding(unhealthy_pods,      action=clean_orphans)
  5. Simulation service offline   → finding(service_offline,    action=start_service)
  6. Docker Compose stack health  → informational only (no action)

auto_remediate / remediate_all
-------------------------------
  action='clean_orphans'  → direct_quick_cleanup(dry_run=False)
  action='start_service'  → _restart_service()  (kills stale 5002 PID, spawns new process)
  action='k8s_recover'    → diagnose_and_recover()  [left in code but no finding uses it]
  (no action key)         → skipped silently by remediate_all

To add a new drift check
------------------------
  1. Add a probe block in ``run_drift_checks()`` (follow the pattern).
  2. Build a finding dict via ``_finding(severity, check, summary, remediation, action=None)``.
  3. If auto-fixable, add a branch in ``auto_remediate()`` for the new action string.
  4. Add a test in ``tests/test_smart_diagnostics.py`` mocking the new probe.
"""

import socket
import subprocess
import sys
import time

from simulation_service_tool.services.command_runner import run_cli_command
from simulation_service_tool.services.direct_cleanup import (
    direct_quick_cleanup,
    direct_verify_state,
    get_test_releases,
)


def _finding(severity, check, summary, remediation, action=None):
    """Build a diagnostic finding dict."""
    return {
        'severity': severity,
        'check': check,
        'summary': summary,
        'remediation': remediation,
        'action': action,
    }


def _docker_running() -> bool:
    """Fast check: can we reach the Docker daemon socket?"""
    # macOS / Linux: test the Unix socket directly (< 1 ms)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect('/var/run/docker.sock')
        sock.close()
        return True
    except OSError:
        pass
    # Fallback for environments where the socket path differs
    try:
        result = subprocess.run(
            ['docker', 'info'],
            capture_output=True, timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


def run_drift_checks(service_running=None):
    """Detect drift from initialized baseline.

    Runs lightweight kubectl/helm checks and returns a list of findings,
    each with severity, summary, remediation text, and an optional
    auto-fix action key.
    """
    if service_running is None:
        from simulation_service_tool.services.api_client import check_service
        service_running = check_service()

    findings = []

    # --- Docker / container runtime prerequisite ---
    # All kubectl/helm checks require Docker Desktop to be running.
    # Detect early so we don't hang on TLS timeouts below.
    if not _docker_running():
        findings.append(_finding(
            severity='error',
            check='docker_not_running',
            summary='Docker Desktop is not running — Kubernetes cluster unavailable',
            remediation='Start Docker Desktop, then ensure Kubernetes is enabled under Settings → Kubernetes.',
            action=None,
        ))
        # k8s-dependent checks would hang; return immediately
        if not service_running:
            findings.append(_finding(
                severity='info',
                check='service_offline',
                summary='Simulation service is offline',
                remediation='Start the service for API-driven test management.',
                action='start_service',
            ))
        return findings

    # --- Kubernetes API reachability ---
    # Docker is running, but the K8s API may still be unreachable (e.g.
    # Kubernetes disabled in Docker Desktop, minikube stopped, etc.)
    from simulation_service_tool.services.k8s_connectivity import k8s_reachable
    k8s_status = k8s_reachable()
    if k8s_status != "reachable":
        findings.append(_finding(
            severity='warning',
            check='k8s_unreachable',
            summary=f'Kubernetes API unreachable ({k8s_status})',
            remediation='Open Diagnostics → K8s Connectivity to scan contexts and recover manually.',
        ))
        # kubectl/helm checks below would time out; skip them
        if not service_running:
            findings.append(_finding(
                severity='info',
                check='service_offline',
                summary='Simulation service is offline',
                remediation='Start the service for API-driven test management.',
                action='start_service',
            ))
        return findings

    # --- Cluster resource drift + pod drift (parallel) ---
    from concurrent.futures import ThreadPoolExecutor

    def _stale_pods():
        return run_cli_command(
            ["kubectl", "get", "pods", "-l", "app=playwright-agent",
             "--field-selector=status.phase!=Running",
             "-o", "jsonpath={.items[*].metadata.name}"],
            timeout=5,
        )

    def _sick_pods():
        return run_cli_command(
            ["kubectl", "get", "pods", "-l", "app=playwright-agent",
             "--field-selector=status.phase=Running",
             "-o", "jsonpath={range .items[*]}{.metadata.name} {range .status.containerStatuses[*]}{.ready}{end}{'\\n'}{end}"],
            timeout=5,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_state = pool.submit(direct_verify_state)
        f_stale = pool.submit(_stale_pods)
        f_sick = pool.submit(_sick_pods)

        state = f_state.result(timeout=15)
        pod_result = f_stale.result(timeout=10)
        sick_result = f_sick.result(timeout=10)

    if state.get('helm_test_releases', 0) > 0:
        releases = get_test_releases()
        names = ', '.join(releases[:3])
        suffix = f' (+{len(releases) - 3} more)' if len(releases) > 3 else ''
        findings.append(_finding(
            severity='warning',
            check='orphaned_releases',
            summary=f"{len(releases)} orphaned release(s): {names}{suffix}",
            remediation='Clean orphaned releases to restore baseline.',
            action='clean_orphans',
        ))

    if state.get('playwright_pvcs', 0) > 0:
        findings.append(_finding(
            severity='warning',
            check='orphaned_pvcs',
            summary='Orphaned PVC (playwright-cache) blocking deployments',
            remediation='Delete the PVC to free cluster resources.',
            action='clean_orphans',
        ))

    if state.get('conflicting_pdbs', 0) > 0:
        findings.append(_finding(
            severity='warning',
            check='conflicting_pdbs',
            summary='Conflicting PDB (playwright-agent-pdb) will block new pods',
            remediation='Delete the PDB to unblock future deployments.',
            action='clean_orphans',
        ))

    # --- Pod drift (residual pods from finished/failed tests) ---
    if pod_result.returncode == 0 and pod_result.stdout.strip():
        stale_pods = pod_result.stdout.strip().split()
        findings.append(_finding(
            severity='warning',
            check='residual_pods',
            summary=f"{len(stale_pods)} non-running pod(s) left behind",
            remediation='Clean residual pods to keep the cluster tidy.',
            action='clean_orphans',
        ))

    # --- Unhealthy running pods ---
    if sick_result.returncode == 0 and sick_result.stdout.strip():
        unhealthy = [
            line.split()[0]
            for line in sick_result.stdout.strip().splitlines()
            if line.strip() and 'false' in line
        ]
        if unhealthy:
            findings.append(_finding(
                severity='error',
                check='unhealthy_pods',
                summary=f"{len(unhealthy)} running pod(s) reporting not-ready",
                remediation='Inspect pod logs or delete unhealthy pods.',
                action='clean_orphans',
            ))

    # --- Service connectivity ---
    if not service_running:
        findings.append(_finding(
            severity='info',
            check='service_offline',
            summary='Simulation service is offline',
            remediation='Start the service for API-driven test management.',
            action='start_service',
        ))

    # --- Docker Compose stack status (informational) ---
    from simulation_service_tool.services.docker_compose import (
        compose_file_exists,
        compose_file_path,
        is_compose_running,
        get_service_health,
        EXPECTED_SERVICES,
    )
    if not compose_file_exists():
        findings.append(_finding(
            severity='warning',
            check='compose_file_missing',
            summary=f"docker-compose.yml not found at {compose_file_path()}",
            remediation='Run the CLI from the project root, or set SIMULATION_COMPOSE_FILE to the correct path.',
        ))
    elif compose_file_exists():
        if is_compose_running():
            health = get_service_health()
            if '_error' not in health:
                down_services = [
                    svc for svc in EXPECTED_SERVICES
                    if not (health.get(svc) or {}).get('running')
                ]
                unhealthy_services = [
                    svc for svc in EXPECTED_SERVICES
                    if (health.get(svc) or {}).get('health') == 'unhealthy'
                ]
                if unhealthy_services:
                    findings.append(_finding(
                        severity='warning',
                        check='docker_unhealthy',
                        summary=f"Docker: {len(unhealthy_services)} unhealthy service(s): {', '.join(unhealthy_services)}",
                        remediation='Open Docker Compose menu to inspect logs and restart.',
                    ))
                elif down_services:
                    findings.append(_finding(
                        severity='info',
                        check='docker_partial',
                        summary=f"Docker: {len(down_services)} service(s) not running: {', '.join(down_services)}",
                        remediation='Open Docker Compose menu to start missing services.',
                    ))

    return findings


def has_drift(service_running=None):
    """Quick boolean — any warning-or-above drift detected?"""
    findings = run_drift_checks(service_running)
    return any(f['severity'] in ('warning', 'error') for f in findings)


def get_drift_banner(findings):
    """Build a one-line banner string summarizing drift, or None if clean."""
    warnings = [f for f in findings if f['severity'] in ('warning', 'error')]
    if not warnings:
        return None
    if len(warnings) == 1:
        return warnings[0]['summary']
    return f"{len(warnings)} issue(s) detected since initialization"


def auto_remediate(finding, service_running=None):
    """Attempt automatic remediation of a single finding.

    Returns (success: bool, detail: str).
    """
    action = finding.get('action')

    if action == 'clean_orphans':
        result = direct_quick_cleanup(dry_run=False)
        errors = result.get('errors', [])
        if errors:
            return False, f"Cleanup errors: {'; '.join(errors[:2])}"
        return True, 'Orphaned resources cleaned'

    if action == 'start_service':
        return _restart_service()

    if action == 'k8s_recover':
        from simulation_service_tool.services.k8s_connectivity import diagnose_and_recover
        recovered = diagnose_and_recover()
        if recovered:
            return True, 'Kubernetes API recovered — switched to a working context'
        return False, 'Kubernetes API recovery failed — see diagnostic details above'

    return False, 'No auto-fix available.'


def _restart_service():
    """Health-check the simulation service; restart it if unresponsive.

    Non-interactive — suitable for auto-remediation flows.
    Returns (success: bool, detail: str).
    """
    from simulation_service_tool.services.api_client import check_service
    from simulation_service_tool.menus.ports import get_port_status, kill_port

    if check_service():
        return True, 'Service is already healthy'

    port_status = get_port_status('5002')
    if port_status.get('in_use'):
        kill_result = kill_port('5002')
        if kill_result.get('failed_pids'):
            return False, f"Could not release port 5002 (PIDs: {', '.join(kill_result['failed_pids'])})"
        time.sleep(0.5)

    subprocess.Popen(
        [sys.executable, "simulation_service.py", "server", "--port", "5002"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    try:
        for _ in range(4):
            time.sleep(1)
            if check_service():
                return True, 'Service restarted and healthy'
    except KeyboardInterrupt:
        return False, 'Cancelled while waiting for service to start'

    return False, 'Service started but not responding yet — check Diagnostics -> Service Health'


def remediate_all(findings, service_running=None):
    """Attempt to fix all actionable findings in one pass.

    Returns (all_fixed: bool, results: list[(check, success, detail)]).
    """
    results = []
    actions_done = set()
    deferred_service_finding = None

    for finding in findings:
        action = finding.get('action')
        if not action or action in actions_done:
            continue
        if action == 'start_service':
            deferred_service_finding = finding
            continue
        actions_done.add(action)
        success, detail = auto_remediate(finding, service_running)
        results.append((finding['check'], success, detail))

    # Run service restart last so cleanup has finished first.
    if deferred_service_finding and 'start_service' not in actions_done:
        actions_done.add('start_service')
        success, detail = auto_remediate(deferred_service_finding, service_running)
        results.append((deferred_service_finding['check'], success, detail))

    all_fixed = all(success for _, success, _ in results) if results else True
    return all_fixed, results
