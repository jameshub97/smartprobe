"""Welcome and top-level navigation menus.

Menu state machine
------------------
``welcome_menu()`` re-evaluates state on every loop iteration:

  State flags
  ~~~~~~~~~~~
  service_running      api_client.check_service()          — hits :5002/health
  cluster_initialized  cluster_init.is_initialized()       — kubectl/helm probe
  drift_findings       smart_diagnostics.run_drift_checks() — only when initialized
  needs_cleanup        unhealthy_pods | orphaned_count | stale_pod

  Layout selected
  ~~~~~~~~~~~~~~~
  not initialized + needs_cleanup  →  menu A: "Initialize Cluster" is option 1
  not initialized + clean          →  menu B: lite menu, no drift banner
  initialized     + drift          →  menu C: Baseline Drift banner + "Fix All Drift Issues" as option 1
  initialized     + no drift      →  menu D: full streamlined menu (most options)

  Dispatch table
  ~~~~~~~~~~~~~~
  ``_handle_welcome_choice()`` maps choice strings → lambda actions.
  Each menu variant has its own mapping block — when editing menu items,
  update *both* the ``choices`` list and the corresponding ``actions`` dict.

Drift remediation flow
----------------------
  User picks "Fix All Drift Issues"
    → _fix_drift(drift_findings, service_running)
    → smart_diagnostics.remediate_all(findings)
    → iterates findings, calls auto_remediate(finding) per action key
    → prints per-finding [OK] / [WARN] results
    → findings with no action key are silently skipped

Kill switch
-----------
  'K) Kill All Pods' appears in all four menu variants.
  _kill_switch_action() prompts nuke/pods-only/cancel,
  then calls services/kill_switch.py nuke_all() or kill_all_pods().
"""

import questionary

from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen
from simulation_service_tool.ui.display import render_main_menu, render_welcome_screen, render_drift_banner, show_loading_spinner
from simulation_service_tool.services.api_client import check_service
from simulation_service_tool.services.cluster_init import is_initialized
from simulation_service_tool.services.smart_diagnostics import run_drift_checks, get_drift_banner, remediate_all
from simulation_service_tool.cli.commands import (
    get_welcome_snapshot,
    hard_reset,
    initialize_cluster_menu,
    start_test_menu,
    stop_test_menu,
    list_tests,
    watch_progress,
    start_service,
)
from simulation_service_tool.menus.cleanup import cleanup_menu
from simulation_service_tool.menus.presets import show_presets
from simulation_service_tool.menus.diagnostics import diagnostics_menu
from simulation_service_tool.menus.docker import docker_menu
from simulation_service_tool.menus.routine_checks import routine_checks_menu
from simulation_service_tool.menus.monitoring import monitoring_menu
from simulation_service_tool.menus.kueue import kueue_menu
from simulation_service_tool.services.kill_switch import get_active_pods, kill_all_pods, nuke_all, probe_kill_switch_targets


def welcome_menu():
    while True:
        clear_screen()
        service_running = check_service()
        cluster_initialized = is_initialized()
        drift_findings = None
        overview = render_welcome_screen(
            service_running,
            lambda progress_callback=None: get_welcome_snapshot(
                service_running,
                progress_callback=progress_callback,
            ),
            message="Connecting to Kubernetes...",
        )
        overview = overview or {}
        unhealthy_pods = overview.get('unhealthy_pods', 0)
        needs_cleanup = unhealthy_pods or overview.get('orphaned_count', 0) or (overview.get('stale_pod') or {}).get('is_stale')

        if not cluster_initialized and needs_cleanup:
            # Not initialized + dirty cluster: prompt init first
            choices = [
                "1) Initialize Cluster (recommended)",
                "2) Start a Test",
                "3) Watch Progress",
                questionary.Separator(),
                "4) Cleanup Center",
                "5) Start Service",
                "6) Docker Compose",
                "7) Monitoring",
                "8) Kueue",
                "9) Diagnostics",
                questionary.Separator(),
                "K) Kill All Pods",
                questionary.Separator(),
                "0) Exit",
            ]
        elif not cluster_initialized:
            # Not initialized but looks clean
            choices = [
                "1) Start a Test",
                "2) Watch Progress",
                "3) Show Presets",
                questionary.Separator(),
                "4) Start Service",
                "5) Docker Compose",
                "6) Initialize Cluster",
                "7) Monitoring",
                "8) Kueue",
                "9) Diagnostics",
                questionary.Separator(),
                "K) Kill All Pods",
                questionary.Separator(),
                "0) Exit",
            ]
        else:
            # Initialized — run drift detection
            drift_findings = run_drift_checks(service_running)
            drift_banner = get_drift_banner(drift_findings)
            # Only offer "Fix All" when at least one finding has an auto-fix action.
            # Informational-only drift (e.g. K8s unreachable) still shows the
            # banner but drops back to the standard menu layout.
            actionable_drift = drift_banner and any(
                f.get('action') for f in drift_findings
            )

            if drift_banner:
                render_drift_banner(drift_banner, drift_findings)

            if actionable_drift:
                choices = [
                    "1) Fix All Drift Issues (recommended)",
                    "2) Start a Test",
                    "3) Stop a Test",
                    "4) Watch Progress",
                    questionary.Separator(),
                    "5) Start Service",
                    "6) Docker Compose",
                    "7) Monitoring",
                    "8) Kueue",
                    "9) Diagnostics",
                    questionary.Separator(),
                    "K) Kill All Pods",
                    questionary.Separator(),
                    "0) Exit",
                ]
            else:
                # Initialized, no actionable drift — clean, streamlined menu
                choices = [
                    "1) Start a Test",
                    "2) Stop a Test",
                    "3) Watch Progress",
                    "4) Show Presets",
                    questionary.Separator(),
                    "5) Start Service",
                    "6) Docker Compose",
                    "7) Initialize Cluster",
                    "8) Monitoring",
                    "9) Kueue",
                    "10) Diagnostics",
                    questionary.Separator(),
                    "K) Kill All Pods",
                    questionary.Separator(),
                    "0) Exit",
                ]
        try:
            choice = questionary.select(
                "What would you like to do?",
                choices=choices,
                style=custom_style,
            ).ask()
        except KeyboardInterrupt:
            print("\nGoodbye!")
            return

        if choice is None:
            # questionary returns None on Ctrl+C or terminal issues
            print("\nGoodbye!")
            return
        if choice.startswith('0'):
            print("\nGoodbye!")
            return
        _handle_welcome_choice(
            choice, service_running, cluster_initialized, needs_cleanup,
            drift_findings=drift_findings if cluster_initialized else None,
        )


def _handle_welcome_choice(choice, service_running, cluster_initialized, needs_cleanup, drift_findings=None):
    option = choice.split(')')[0].strip()
    # Mirror the same actionable-drift check used when building choices above.
    has_drift = bool(
        drift_findings
        and get_drift_banner(drift_findings)
        and any(f.get('action') for f in drift_findings)
    )

    if not cluster_initialized and needs_cleanup:
        # Menu layout: 1=Init, 2=Start, 3=Watch, 4=Cleanup, 5=Service, 6=Docker, 7=Monitoring, 8=Kueue, 9=Diag
        actions = {
            '1': lambda: initialize_cluster_menu(),
            '2': lambda: start_test_menu(service_running),
            '3': lambda: watch_progress(service_running),
            '4': lambda: cleanup_menu(),
            '5': lambda: start_service(),
            '6': lambda: docker_menu(),
            '7': lambda: monitoring_menu(),
            '8': lambda: kueue_menu(),
            '9': lambda: diagnostics_menu(service_running),
            'K': lambda: _kill_switch_action(),
        }
    elif not cluster_initialized:
        # Menu layout: 1=Start, 2=Watch, 3=Presets, 4=Service, 5=Docker, 6=Init, 7=Monitoring, 8=Kueue, 9=Diag
        actions = {
            '1': lambda: start_test_menu(service_running),
            '2': lambda: watch_progress(service_running),
            '3': lambda: show_presets(),
            '4': lambda: start_service(),
            '5': lambda: docker_menu(),
            '6': lambda: initialize_cluster_menu(),
            '7': lambda: monitoring_menu(),
            '8': lambda: kueue_menu(),
            '9': lambda: diagnostics_menu(service_running),
            'K': lambda: _kill_switch_action(),
        }
    elif has_drift:
        # Menu layout: 1=FixDrift, 2=Start, 3=Stop, 4=Watch, 5=Service, 6=Docker, 7=Monitoring, 8=Kueue, 9=Diag
        actions = {
            '1': lambda: _fix_drift(drift_findings, service_running),
            '2': lambda: start_test_menu(service_running),
            '3': lambda: stop_test_menu(service_running),
            '4': lambda: watch_progress(service_running),
            '5': lambda: start_service(),
            '6': lambda: docker_menu(),
            '7': lambda: monitoring_menu(),
            '8': lambda: kueue_menu(),
            '9': lambda: diagnostics_menu(service_running),
            'K': lambda: _kill_switch_action(),
        }
    else:
        # Menu layout: 1=Start, 2=Stop, 3=Watch, 4=Presets, 5=Service, 6=Docker, 7=Init, 8=Monitoring, 9=Kueue, 10=Diag
        actions = {
            '1': lambda: start_test_menu(service_running),
            '2': lambda: stop_test_menu(service_running),
            '3': lambda: watch_progress(service_running),
            '4': lambda: show_presets(),
            '5': lambda: start_service(),
            '6': lambda: docker_menu(),
            '7': lambda: initialize_cluster_menu(),
            '8': lambda: monitoring_menu(),
            '9': lambda: kueue_menu(),
            '10': lambda: diagnostics_menu(service_running),
            'K': lambda: _kill_switch_action(),
        }

    action = actions.get(option)
    if action:
        action()


def _kill_switch_action():
    """Emergency kill switch — confirm then nuke all pods and releases."""
    clear_screen()
    red = "\033[31m"
    bold = "\033[1m"
    dim = "\033[2m"
    reset = "\033[0m"

    print(f"\n  {red}{bold}KILL SWITCH{reset}")
    print(f"  {dim}Force-delete all pods and Helm releases in the default namespace.{reset}\n")

    target_state = show_loading_spinner(
        probe_kill_switch_targets,
        message="Checking kill switch targets...",
    )
    if target_state is None:
        print("  Kill switch probe cancelled.")
        _prompt_go_back()
        return

    pods = target_state["pods"]
    releases = target_state["releases"]
    pod_count = target_state["pod_count"]
    release_count = target_state["release_count"]

    print(
        f"  Probe complete: {pod_count} active pod(s), "
        f"{release_count} Helm release(s)."
    )

    if not target_state["has_targets"]:
        print("  No active pods or Helm releases found in default namespace.")
        _prompt_go_back()
        return

    if pods:
        print(f"\n  Active pods ({pod_count}):")
        for pod in pods:
            print(f"    {dim}·{reset} {pod['name']}  {dim}{pod['status']}{reset}")

    if releases:
        print(f"\n  Helm releases ({release_count}):")
        for release in releases:
            print(f"    {dim}·{reset} {release}")

    if pod_count and release_count:
        prompt = f"\n  Delete all {pod_count} pod(s) and uninstall {release_count} Helm release(s)?"
        choices = [
            questionary.Choice(title="Yes — nuke everything", value="nuke"),
            questionary.Choice(title="Pods only — keep Helm releases", value="pods"),
            questionary.Choice(title="Cancel", value="cancel"),
        ]
    elif pod_count:
        prompt = f"\n  Delete all {pod_count} pod(s)?"
        choices = [
            questionary.Choice(title="Yes — delete all pods", value="pods"),
            questionary.Choice(title="Cancel", value="cancel"),
        ]
    else:
        prompt = f"\n  Uninstall all {release_count} Helm release(s)?"
        choices = [
            questionary.Choice(title="Yes — remove all Helm releases", value="nuke"),
            questionary.Choice(title="Cancel", value="cancel"),
        ]

    confirm = questionary.select(
        prompt,
        choices=choices,
        style=custom_style,
    ).ask()

    if confirm == "nuke":
        if pod_count and release_count:
            print(f"\n  {bold}Nuking all releases and pods...{reset}")
        else:
            print(f"\n  {bold}Removing Helm releases...{reset}")
        result = nuke_all()
        print(f"  Helm releases removed: {result['releases_removed']}")
        print(f"  Pods deleted: {result['pods_deleted']}")
        if result["errors"]:
            for err in result["errors"]:
                print(f"  {red}error: {err}{reset}")
        else:
            print(f"\n  {bold}Done.{reset}")
    elif confirm == "pods":
        print(f"\n  {bold}Killing all pods...{reset}")
        result = kill_all_pods()
        print(f"  Pods deleted: {result['deleted']}")
        if result["errors"]:
            for err in result["errors"]:
                print(f"  {red}error: {err}{reset}")
        else:
            print(f"\n  {bold}Done.{reset}")
    else:
        print("  Cancelled.")

    _prompt_go_back()


def _fix_drift(findings, service_running):
    """Auto-remediate all drift findings and report results."""
    clear_screen()
    print("+" + "=" * 62 + "+")
    print("|              FIXING DRIFT ISSUES                             |")
    print("+" + "=" * 62 + "+")

    warnings = [f for f in findings if f['severity'] in ('warning', 'error')]
    if not warnings:
        print("\n  [OK] No drift issues to fix.")
        _prompt_go_back()
        return

    print(f"\n  Remediating {len(warnings)} issue(s)...\n")
    all_fixed, results = remediate_all(findings, service_running)

    for check, success, detail in results:
        status = "[OK]" if success else "[WARN]"
        print(f"  {status} {check}: {detail}")

    if all_fixed:
        print("\n  [OK] All drift issues resolved. Baseline restored.")
    else:
        print("\n  [WARN] Some issues could not be auto-fixed.")
        print("  Open Diagnostics or Re-initialize for a full reset.")

    _prompt_go_back()


def _prompt_go_back():
    questionary.select(
        "Next step:",
        choices=[questionary.Choice(title="Go back", value="back")],
        style=custom_style,
    ).ask()


def test_operations_menu(service_running):
    while True:
        clear_screen()
        render_main_menu(service_running)
        choice = questionary.select(
            "Test operations",
            choices=[
                "1) Start a Test",
                "2) Stop a Test",
                "3) List Tests",
                "4) Watch Progress",
                "5) Show Presets",
                questionary.Separator(),
                "0) Back",
            ],
            style=custom_style,
        ).ask()

        if not choice or choice.startswith('0'):
            return

        option = choice.split(')')[0]
        if option == '1':
            start_test_menu(service_running)
        elif option == '2':
            stop_test_menu(service_running)
        elif option == '3':
            list_tests(service_running)
        elif option == '4':
            watch_progress(service_running)
        elif option == '5':
            show_presets()