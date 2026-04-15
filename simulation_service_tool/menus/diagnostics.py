"""Diagnostics menu — simplified single-screen flow."""

import questionary

from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen
from simulation_service_tool.services.api_client import check_service
from simulation_service_tool.services.smart_diagnostics import run_drift_checks, remediate_all
from simulation_service_tool.services.k8s_connectivity import diagnose, diagnose_and_recover
from simulation_service_tool.services.docker_compose import (
    compose_file_exists,
    is_compose_running,
    get_service_health,
    test_endpoints,
    EXPECTED_SERVICES,
)
from simulation_service_tool.cli.commands import start_service


def _prompt_continue():
    """Simple continue prompt."""
    questionary.select(
        "Next:",
        choices=[questionary.Choice(title="Continue", value="continue")],
        style=custom_style,
    ).ask()


def quick_diagnostics() -> dict:
    """Run essential checks only — fast via parallel probes."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_service = pool.submit(check_service)
        f_compose = pool.submit(lambda: is_compose_running() if compose_file_exists() else False)
        f_endpoints = pool.submit(test_endpoints)
        f_k8s = pool.submit(diagnose)

        service_running = f_service.result(timeout=10)
        compose_running = f_compose.result(timeout=15)
        endpoints = f_endpoints.result(timeout=15)
        k8s_diag = f_k8s.result(timeout=30)

    compose_health = {}
    if compose_running:
        compose_health = get_service_health()

    return {
        'service_running': service_running,
        'drift_findings': run_drift_checks(service_running),
        'k8s_diag': k8s_diag,
        'compose_running': compose_running,
        'compose_health': compose_health,
        'endpoints': endpoints,
    }


def render_simple_diagnostics(diag: dict) -> list:
    """Single, clean diagnostic view.  Returns the list of warning/error issues."""
    clear_screen()

    W = 50
    print("╔" + "═" * W + "╗")
    print("║" + "  DIAGNOSTICS".center(W) + "║")
    print("╠" + "═" * W + "╣")

    # Service status
    status = "✓ Running" if diag['service_running'] else "✗ Offline"
    print(f"║  Service:     {status:<{W - 16}}║")

    # K8s status
    k8s = diag['k8s_diag']
    k8s_status = "✓ Reachable" if k8s['status'] == 'healthy' else "✗ Unreachable"
    print(f"║  Kubernetes:  {k8s_status:<{W - 16}}║")

    # Docker Compose status
    if diag.get('compose_running'):
        health = diag.get('compose_health', {})
        down_svcs = [s for s in EXPECTED_SERVICES if not (health.get(s) or {}).get('running')]
        if down_svcs:
            compose_label = f"✗ {len(down_svcs)} service(s) down"
        else:
            compose_label = "✓ All services running"
    elif compose_file_exists():
        compose_label = "✗ Stack not running"
    else:
        compose_label = "- No compose file"
    print(f"║  Docker:      {compose_label:<{W - 16}}║")

    # Backend API endpoints
    endpoints = diag.get('endpoints', [])
    if endpoints:
        healthy_eps = [e for e in endpoints if e.get('healthy')]
        if len(healthy_eps) == len(endpoints):
            ep_label = f"✓ {len(endpoints)}/{len(endpoints)} endpoints healthy"
        else:
            ep_label = f"✗ {len(healthy_eps)}/{len(endpoints)} endpoints healthy"
        print(f"║  Backend API: {ep_label:<{W - 16}}║")

    # Drift summary
    drift = diag['drift_findings']
    issues = [f for f in drift if f.get('severity') in ('warning', 'error')]
    if issues:
        print("╠" + "═" * W + "╣")
        header = f"  Issues found ({len(issues)}):"
        print(f"║{header:<{W}}║")
        for issue in issues[:5]:
            summary = issue['summary']
            if len(summary) > W - 8:
                summary = summary[:W - 11] + "..."
            print(f"║    • {summary:<{W - 6}}║")

    print("╚" + "═" * W + "╝")

    return issues


def _auto_fix(issues, diag):
    """Single remediation flow.  Returns True if anything was fixed."""
    fixed = False

    # Fix drift issues
    if issues:
        print("\n[INFO] Fixing drift issues...")
        _, results = remediate_all(diag['drift_findings'])
        for _, success, detail in results:
            marker = "[OK]" if success else "[WARN]"
            print(f"   {marker} {detail}")
            if success:
                fixed = True

    # Fix K8s if needed
    if diag['k8s_diag']['status'] != 'healthy':
        print("\n[INFO] Recovering Kubernetes...")
        if diagnose_and_recover():
            print("   [OK] K8s is now reachable")
            fixed = True
        else:
            print("   [WARN] Could not recover K8s automatically")

    # Start service if needed
    if not diag['service_running']:
        if questionary.confirm("Start simulation service?", default=True, style=custom_style).ask():
            start_service()
            fixed = True

    return fixed


def diagnostics_menu(service_running=None):
    """Simplified diagnostics — one screen, one flow."""
    while True:
        print("\n[INFO] Running diagnostics...")
        diag = quick_diagnostics()
        issues = render_simple_diagnostics(diag)

        # Build choices
        choices = []
        if issues or diag['k8s_diag']['status'] != 'healthy' or not diag['service_running']:
            choices.append(questionary.Choice(
                title="Fix all issues",
                value="fix",
            ))
        choices.extend([
            questionary.Choice(title="Run diagnostics again", value="retry"),
            questionary.Separator(),
            questionary.Choice(title="Back", value="back"),
        ])

        action = questionary.select(
            "What would you like to do?",
            choices=choices,
            style=custom_style,
        ).ask()

        if not action or action == "back":
            return
        if action == "retry":
            continue
        if action == "fix":
            try:
                if _auto_fix(issues, diag):
                    print("\n[OK] Issues fixed. Re-running diagnostics...")
                    continue
                else:
                    print("\n[WARN] Some issues could not be fixed automatically.")
                    _prompt_continue()
            except KeyboardInterrupt:
                print("\n[INFO] Fix cancelled.")
                _prompt_continue()
