"""Monitoring stack management — simplified."""

import questionary

from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen
from simulation_service_tool.services.monitoring import (
    is_helm_available,
    is_monitoring_installed,
    install_stack,
    uninstall_stack,
    get_stack_status,
    get_monitoring_pods,
    get_grafana_access,
    get_prometheus_targets,
    apply_servicemonitor,
)
from simulation_service_tool.cli.prompts import _prompt_continue


def monitoring_menu():
    """Simplified monitoring menu — single loop, inline actions."""
    while True:
        clear_screen()
        W = 50
        print("╔" + "═" * W + "╗")
        print("║" + "  MONITORING (Prometheus / Grafana)".center(W) + "║")
        print("╠" + "═" * W + "╣")

        if not is_helm_available():
            print(f"║  {'[WARN] Helm not installed':<{W}}║")
            print("╚" + "═" * W + "╝")
            print("\n  Install: https://helm.sh/docs/intro/install/")
            _prompt_continue()
            return

        installed = is_monitoring_installed()
        status_label = "✓ Installed" if installed else "○ Not installed"
        print(f"║  Status:  {status_label:<{W - 12}}║")

        if installed:
            s = get_stack_status()
            print(f"║  Version: {str(s.get('version', '?')):<{W - 12}}║")

        print("╚" + "═" * W + "╝")

        choices = _build_choices(installed)

        try:
            action = questionary.select(
                "Monitoring action:",
                choices=choices,
                style=custom_style,
            ).ask()
        except KeyboardInterrupt:
            return

        if not action or action == "back":
            return

        _handle_action(action)


def _build_choices(installed: bool) -> list:
    """Build choices — only show relevant options."""
    if not installed:
        return [
            questionary.Choice(title="Install monitoring stack", value="install"),
            questionary.Choice(title="Apply ServiceMonitor & Alerts", value="monitors"),
            questionary.Separator(),
            questionary.Choice(title="Back", value="back"),
        ]
    return [
        questionary.Choice(title="View pods", value="status"),
        questionary.Choice(title="Grafana access", value="grafana"),
        questionary.Choice(title="Prometheus access", value="prometheus"),
        questionary.Choice(title="Apply ServiceMonitor & Alerts", value="monitors"),
        questionary.Separator(),
        questionary.Choice(title="Uninstall stack", value="uninstall"),
        questionary.Separator(),
        questionary.Choice(title="Back", value="back"),
    ]


def _handle_action(action: str):
    """Execute a single monitoring action."""
    if action == "install":
        print("\n  [INFO] Installing kube-prometheus-stack...")
        result = install_stack()
        if result["success"]:
            print("  [OK] Installed. Applying ServiceMonitor...")
            apply_servicemonitor()
            print("  [OK] Ready.")
        else:
            print("  [FAIL] Installation failed.")
            if result.get("stderr"):
                for line in result["stderr"].strip().splitlines()[:5]:
                    print(f"    {line}")

    elif action == "status":
        pods = get_monitoring_pods()
        if not pods:
            print("\n  No pods found in monitoring namespace.")
        else:
            print()
            for p in pods[:15]:
                print(f"  {p['name']:<45} {p['ready']}  {p['status']}")

    elif action == "grafana":
        info = get_grafana_access()
        print(f"\n  Run:  {info['command']}")
        print(f"  URL:  {info['url']}")
        print(f"  User: {info['credentials']['username']}")
        print(f"  Pass: {info['credentials']['password']}")

    elif action == "prometheus":
        info = get_prometheus_targets()
        print(f"\n  Run:  {info['command']}")
        print(f"  URL:  {info['url']}")

    elif action == "monitors":
        print("\n  Applying ServiceMonitor and PrometheusRule manifests...")
        result = apply_servicemonitor()
        for r in result["results"]:
            marker = "[OK]" if r["success"] else "[FAIL]"
            print(f"  {marker} {r['manifest']}")

    elif action == "uninstall":
        confirm = questionary.confirm(
            "Remove monitoring stack? This deletes Prometheus, Grafana, and all data.",
            default=False,
            style=custom_style,
        ).ask()
        if not confirm:
            return
        print("\n  Uninstalling monitoring stack...")
        result = uninstall_stack()
        if result["success"]:
            print("  [OK] Monitoring stack removed.")
        else:
            print("  [FAIL] Uninstall failed.")
            if result.get("stderr"):
                for line in result["stderr"].strip().splitlines()[:5]:
                    print(f"    {line}")

    _prompt_continue()
