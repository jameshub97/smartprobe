"""Preflight orchestration for test startup.

This module keeps the user-facing flow together while lower-level helpers live
in sibling modules:
    - ``preflight_support.py`` for pure helpers and runtime probes
    - ``initialize_cluster.py`` for the cluster-init menu UI
"""

import re
import sys

import questionary

from simulation_service_tool.cli.prompts import _prompt_go_back
from simulation_service_tool.cli.preflight_support import (
    check_docker_services as _check_docker_services_impl,
    clear_hung_api_listeners_before_preflight as _clear_hung_api_listeners_before_preflight_impl,
    extract_conflicting_release as _extract_conflicting_release_impl,
    fallback_info_lines as _fallback_info_lines_impl,
    probe_sim_api as _probe_sim_api_impl,
    should_fallback_to_direct as _should_fallback_to_direct_impl,
)
from simulation_service_tool.services.command_runner import run_cli_command
from simulation_service_tool.services.api_client import call_service
from simulation_service_tool.services.direct_cleanup import (
    direct_preflight_check,
    direct_release_cleanup,
    direct_stuck_cleanup,
    direct_verify_state,
    get_quick_cleanup_commands,
)
from simulation_service_tool.services.hung_api_cleanup import clear_hung_api_listeners
from simulation_service_tool.ui.display import render_status_summary
from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen


def _extract_conflicting_release(error_message):
    return _extract_conflicting_release_impl(error_message)


def _auto_fix_conflicts(preflight):
    """Auto-fix all known conflict types directly (no API, no prompts).

    Handles releases, PVCs, and PDBs — everything preflight can detect.
    Returns True if any cleanup was attempted.
    """
    conflicts = preflight.get('conflicts', [])
    if not conflicts:
        return False

    fixed_any = False
    for conflict in conflicts:
        ctype = conflict.get('type')
        if ctype == 'helm_releases':
            for release in conflict.get('releases', []):
                if release:
                    direct_release_cleanup(release, dry_run=False)
                    fixed_any = True
        elif ctype == 'pvc':
            name = conflict.get('name', 'playwright-cache')
            run_cli_command(["kubectl", "delete", "pvc", name, "--ignore-not-found"], timeout=10)
            fixed_any = True
        elif ctype == 'pdb':
            name = conflict.get('name', 'playwright-agent-pdb')
            run_cli_command(["kubectl", "delete", "pdb", name, "--ignore-not-found"], timeout=10)
            fixed_any = True
    return fixed_any


def _print_preflight_conflicts(preflight, indent=''):
    print(f"\n{indent}[33m[WARN][0m Conflicts detected:\n")
    for conflict in preflight.get('conflicts', []):
        cname = conflict.get('name', ', '.join(conflict.get('releases', ['unknown'])))
        print(f"{indent}   - {conflict['type']}: {cname}")
        print(f"{indent}     Fix: {conflict['fix']}")


def _handle_remaining_preflight_conflicts(preflight, service_running, allow_force=True):
    from simulation_service_tool.menus.cleanup import cleanup_menu
    from simulation_service_tool.cli.initialize_cluster import initialize_cluster_menu

    while True:
        _print_preflight_conflicts(preflight)
        print("\n[36m[INFO][0m Initialization and auto-fix already handled the standard blockers.")
        print("[36m[INFO][0m Use Cleanup Center or re-initialize if the cluster has residual data.")

        choices = [
            questionary.Choice(title="Open Cleanup Center", value="cleanup"),
            questionary.Choice(title="Re-initialize Cluster", value="initialize"),
            questionary.Choice(title="Refresh preflight status", value="refresh"),
        ]
        if allow_force:
            choices.append(questionary.Choice(title="Start anyway (may fail)", value="force"))
        choices.append(questionary.Choice(title="Cancel", value="cancel"))

        action = questionary.select(
            "What would you like to do?",
            choices=choices,
            style=custom_style,
        ).ask()

        if not action or action == 'cancel':
            return False
        if action == 'force':
            return True
        if action == 'cleanup':
            cleanup_menu()
        elif action == 'initialize':
            initialize_cluster_menu()

        refreshed = _get_preflight(service_running)
        if refreshed.get('cancelled'):
            print("[36m[INFO][0m Refresh cancelled. Returning to the conflict menu.")
            continue
        if refreshed.get('error'):
            print(f"[33m[WARN][0m Could not refresh preflight status: {refreshed['error']}")
            return False
        if not refreshed.get('has_conflicts'):
            print("[32m[OK][0m Preflight conflicts cleared. Continuing.")
            return True

        preflight = refreshed


def _should_fallback_to_direct(error_message):
    return _should_fallback_to_direct_impl(error_message)


def _fallback_info_lines(message, error_message, endpoint_label):
    return _fallback_info_lines_impl(message, error_message, endpoint_label)


def _pause_after_fallback(message, error_message=None, endpoint_label=None):
    print(message)
    if not sys.stdin.isatty():
        return 'continue'

    continue_title = 'Continue with direct cleanup'
    has_cleanup_endpoint = bool(endpoint_label and '/api/cleanup/' in endpoint_label)
    if endpoint_label and (has_cleanup_endpoint or endpoint_label == 'GET /api/preflight'):
        continue_title = 'Continue with direct cleanup (recommended)'

    while True:
        choices = [questionary.Choice(title=continue_title, value="continue")]
        if has_cleanup_endpoint:
            choices.extend([
                questionary.Choice(title="Run quick clean now", value="quick_clean"),
                questionary.Choice(title="Show quick-clean commands", value="commands"),
            ])
        choices.extend([
            questionary.Choice(title="Get more info", value="info"),
            questionary.Choice(title="Return to conflict menu", value="cancel"),
        ])

        action = questionary.select(
            "Fallback action:",
            choices=choices,
            style=custom_style,
        ).ask()

        if action == 'info':
            print()
            for line in _fallback_info_lines(message, error_message, endpoint_label):
                print(line)
            print()
            continue

        if action == 'commands':
            print()
            print('[36m[INFO][0m Quick clean commands:')
            for command in get_quick_cleanup_commands():
                print(f"  - {' '.join(command)}")
            print()
            continue

        return action or 'cancel'


def _probe_sim_api(timeout: float = 2.5) -> bool:
    return _probe_sim_api_impl(timeout=timeout)


def _check_docker_services() -> dict:
    return _check_docker_services_impl()


def _clear_hung_api_listeners_before_preflight(service_running: bool) -> bool:
    return _clear_hung_api_listeners_before_preflight_impl(
        service_running,
        cleanup_fn=clear_hung_api_listeners,
        print_fn=print,
    )


def _get_preflight(service_running):
    service_running = _clear_hung_api_listeners_before_preflight(service_running)

    if not service_running:
        return direct_preflight_check()

    # --- Quick API responsiveness probe (avoids a 10 s hang) ----------------
    # The service may accept TCP connections (so check_service() returns True)
    # but be stuck on all real requests.  A tight-timeout ping detects that
    # state and lets us fall back to direct mode immediately.
    if not _probe_sim_api(timeout=2.5):
        print("[33m[WARN][0m Simulation service is not responding (API probe timed out).")
        print("[36m[INFO][0m Falling back to direct preflight checks.")
        return direct_preflight_check()

    # --- Docker services reachability check ----------------------------------
    # Surface any offline services up-front so the user knows what won't work
    # before the test starts, rather than hitting errors mid-test.
    docker_status = _check_docker_services()
    offline = [name for name, up in docker_status.items() if not up]
    if offline:
        services_str = ', '.join(offline)
        print(f"[33m[WARN][0m Service(s) unreachable before preflight: {services_str}")
        print("[36m[INFO][0m Tests require both the simulation service (5002) and backend API (5001).")

    result = call_service('/api/preflight')
    if result.get('error') and _should_fallback_to_direct(result['error']):
        action = _pause_after_fallback(
            "[33m[WARN][0m Preflight endpoint unavailable. Falling back to direct preflight checks.",
            result.get('error'),
            'GET /api/preflight',
        )
        if action == 'cancel':
            return {'cancelled': True, 'error': 'Fallback cancelled. Returned to conflict menu.'}
        return direct_preflight_check()
    return result


_BREW_PACKAGES = {
    "helm": "helm",
    "kubectl": "kubectl",
}

_INSTALL_HINTS = {
    "helm": "https://helm.sh/docs/intro/install/",
    "kubectl": "https://kubernetes.io/docs/tasks/tools/",
}


def _brew_available() -> bool:
    import shutil
    return shutil.which("brew") is not None


def _install_via_brew(binary: str) -> bool:
    """Run `brew install <pkg>` with live output. Returns True on success."""
    import subprocess
    pkg = _BREW_PACKAGES.get(binary, binary)
    print(f"\n\033[1mInstalling {pkg} via Homebrew...\033[0m")
    result = subprocess.run(["brew", "install", pkg], shell=False)
    return result.returncode == 0


def _handle_start_error_recovery(error_message, service_running):
    # Missing dependency errors — cleanup options are irrelevant; offer install instead.
    _lower = (error_message or "").lower()
    if "required command" in _lower and "was not found" in _lower:
        import re as _re
        m = _re.search(r"required command '([^']+)'", error_message, _re.IGNORECASE)
        binary = m.group(1) if m else "helm"
        print(f"\n\033[1mMissing dependency: \033[31m{binary}\033[0m\033[0m")
        print(f"  '{binary}' is not installed or is not on PATH.")
        docs = _INSTALL_HINTS.get(binary)
        if docs:
            print(f"  Docs: {docs}")
        print()

        choices = []
        if _brew_available() and binary in _BREW_PACKAGES:
            choices.append(
                questionary.Choice(
                    title=f"Install {binary} now  (brew install {_BREW_PACKAGES[binary]})",
                    value="brew_install",
                )
            )
        choices.append(questionary.Choice(title="Return to menu", value="back"))

        action = questionary.select(
            f"  '{binary}' is required to start tests. What would you like to do?",
            choices=choices,
            style=custom_style,
        ).ask()

        if action == "brew_install":
            success = _install_via_brew(binary)
            if success:
                import shutil as _shutil
                # Check whether the binary is now resolvable (it will be, since
                # the service now augments PATH to include /opt/homebrew/bin).
                resolved = _shutil.which(binary)
                print(f"\n\033[32m✓ {binary} installed successfully.\033[0m")
                if resolved:
                    print(f"  Found at: {resolved}")
                    print(f"  You can retry starting the test now — go back and select Start a Test.")
                else:
                    print(f"  Installed but not yet on your shell PATH.")
                    print(f"  You can still retry from the menu; the CLI will find it automatically.")
            else:
                print(f"\n\033[33m[WARN]\033[0m Installation may have failed. Check the output above.")
                if docs:
                    print(f"  Manual install: {docs}")
        print()
        _prompt_go_back()
        return

    conflicting_release = _extract_conflicting_release(error_message)
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
            title="Delete shared stuck resources (PVCs / PDBs)",
            value=("cleanup_stuck", None),
        ),
        questionary.Choice(
            title="Refresh cluster status",
            value=("refresh", None),
        ),
        questionary.Choice(
            title="Return to menu",
            value=("back", None),
        ),
    ])

    action = questionary.select(
        "Start failed. What would you like to do next?",
        choices=choices,
        style=custom_style,
    ).ask()

    if not action:
        return

    action_type, value = action

    if action_type == "delete_release":
        if not value or not re.match(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$', value):
            print("[33m[WARN][0m Refusing cleanup for an invalid release name.")
            _prompt_go_back()
            return
        confirm = questionary.confirm(
            f"Delete release '{value}' and its Helm-managed resources?",
            default=False,
        ).ask()
        if not confirm:
            return

        print(f"\nCleaning conflicting release: {value}")
        result = direct_release_cleanup(value, dry_run=False)
        if result.get('error'):
            print(f"[33m[WARN][0m {result['error']}")
        elif result.get('warning'):
            print(f"[33m[WARN][0m {result['warning']}")
        else:
            deleted_anything = result.get('helm') == 'uninstalled' or any(result.get(key) for key in ('pods', 'pvcs', 'pdbs', 'jobs'))
            print("[32m[OK][0m Conflicting release cleanup requested." if deleted_anything else "[33m[WARN][0m No matching release resources were removed.")
    elif action_type == "cleanup_stuck":
        print("\nCleaning shared stuck resources...")
        result = direct_stuck_cleanup(dry_run=False)
        resources_deleted = any(result.get(key) for key in ('stuck_resources', 'orphaned_pvcs', 'conflicting_pdbs'))
        print("[32m[OK][0m Stuck resource cleanup requested." if resources_deleted else "[33m[WARN][0m No stuck resources were found.")
    elif action_type == "refresh":
        state = direct_verify_state()
        render_status_summary(False, state)

    _prompt_go_back()


def preflight_check(service_running):
    """Run preflight conflict detection."""
    clear_screen()
    print("+" + "=" * 62 + "+")
    print("|                     PREFLIGHT CHECK                          |")
    print("+" + "=" * 62 + "+")
    result = None

    if service_running:
        result = _get_preflight(service_running)
        if result.get('cancelled'):
            print("\n  [33m[CANCELLED][0m Preflight check was cancelled.")
        elif result.get('error'):
            print(f"\n  [33m[WARN][0m Could not run preflight checks: {result['error']}")
        elif result.get('has_conflicts'):
            _print_preflight_conflicts(result, indent='  ')
            print("\n  [36m[INFO][0m Attempting standard conflict cleanup...")
            attempted_cleanup = _auto_fix_conflicts(result)
            if attempted_cleanup:
                refreshed = _get_preflight(service_running)
                if refreshed.get('cancelled'):
                    print("\n  [36m[INFO][0m Preflight refresh cancelled.")
                elif refreshed.get('error'):
                    print(f"\n  [33m[WARN][0m Could not refresh preflight checks: {refreshed['error']}")
                else:
                    result = refreshed

        if result.get('has_conflicts'):
            action = questionary.select(
                "What would you like to do next?",
                choices=[
                    questionary.Choice(title="Open Cleanup Center / re-initialize now", value="resolve"),
                    questionary.Choice(title="Return", value="back"),
                ],
                style=custom_style,
            ).ask()

            if action == "resolve":
                resolved = _handle_remaining_preflight_conflicts(result, service_running, allow_force=False)
                if resolved:
                    print("\n  [32m[OK][0m Cluster is now ready for tests.")
                    result = _get_preflight(service_running)
            else:
                print("\n  Return to the main menu when you're ready.")
        elif not result.get('cancelled') and not result.get('error'):
            print("\n  [32m[OK][0m No conflicts. Cluster is ready for tests.")
    else:
        state = direct_verify_state()
        is_clean = state.get('is_clean', False)
        print(f"\n  Test releases: {state.get('helm_test_releases', '?')}")
        print(f"  Playwright pods: {state.get('playwright_pods', '?')}")
        print(f"  Stuck PVCs: {state.get('playwright_pvcs', '?')}")
        print(f"  Conflicting PDBs: {state.get('conflicting_pdbs', '?')}")
        print(f"\n  Status: {'[32m[OK][0m Clean' if is_clean else '[33m[WARN][0m Needs cleanup'}")
    _prompt_go_back()
    return result if service_running else None
