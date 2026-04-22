"""Cleanup menu and handlers."""

import questionary

from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen
from simulation_service_tool.ui.display import (
    display_cleanup_result,
    display_verification_result,
    render_drift_banner,
    render_key_value_panel,
    render_smart_summary_panel,
    show_loading_spinner,
)
from simulation_service_tool.services.api_client import check_service, call_service
from simulation_service_tool.services.smart_diagnostics import run_drift_checks, get_drift_banner
from simulation_service_tool.services.direct_cleanup import (
    direct_quick_cleanup,
    direct_full_cleanup,
    direct_stuck_cleanup,
    direct_release_cleanup,
    direct_completed_pods_cleanup,
    direct_verify_state,
    get_quick_cleanup_commands,
    get_test_releases,
)
from simulation_service_tool.cli.prompts import _prompt_go_back


def _cleanup_issue(summary, remediation):
    return {
        'summary': summary,
        'remediation': remediation,
    }


def ensure_cleanup_loaded(progress_callback=None):
    progress = progress_callback or (lambda _message: None)

    progress("Checking cleanup service availability...")
    service_running = check_service()
    progress("Loading cleanup state snapshot...")

    if service_running:
        payload = call_service('/api/cleanup/preflight')
        state = payload.get('state', {})
        ready = payload.get('ready', False)
    else:
        direct_state = direct_verify_state()
        payload = {'state': direct_state, 'ready': direct_state.get('is_clean', False)}
        state = payload['state']
        ready = payload['ready']

    issues = []
    release_count = state.get('helm_test_releases', 0)
    pod_count = state.get('playwright_pods', 0)
    pvc_count = state.get('playwright_pvcs', 0)
    pdb_count = state.get('conflicting_pdbs', 0)

    if release_count:
        issues.append(_cleanup_issue(
            f"{release_count} test release(s) still installed",
            "Run Quick Clean to remove stale test releases before starting another run.",
        ))
    if pod_count:
        issues.append(_cleanup_issue(
            f"{pod_count} Playwright pod(s) still present",
            "Use Full Reset if old pods are blocking new test agents.",
        ))
    if pvc_count:
        issues.append(_cleanup_issue(
            f"{pvc_count} stale PVC resource(s) detected",
            "Use Clean Stuck Resources to remove leftover storage claims.",
        ))
    if pdb_count:
        issues.append(_cleanup_issue(
            f"{pdb_count} conflicting PDB resource(s) detected",
            "Use Clean Stuck Resources or Full Reset to remove stale disruption budgets.",
        ))

    progress("Cleanup snapshot ready.")
    return {
        'service_running': service_running,
        'state': state,
        'ready': ready,
        'issues': issues,
        'has_issues': bool(issues),
    }


def render_smart_cleanup(cache):
    state = cache.get('state', {})
    rows = [
        ('Service mode', 'API-backed cleanup' if cache.get('service_running') else 'Direct commands only'),
        ('Test releases', str(state.get('helm_test_releases', '?'))),
        ('Playwright pods', str(state.get('playwright_pods', '?'))),
        ('Stuck PVCs', str(state.get('playwright_pvcs', '?'))),
        ('Conflicting PDBs', str(state.get('conflicting_pdbs', '?'))),
        ('Status', '[OK] Clean' if cache.get('ready') else '[WARN] Needs cleanup'),
    ]
    render_key_value_panel('Cleanup Overview', rows)
    render_smart_summary_panel(
        'Cleanup Actions',
        issues=cache.get('issues', []),
        recommendation='Recommended: Quick Clean is the safest first pass when cleanup is needed.',
        healthy_message='Cleanup state is already clear. Verify if you want extra confirmation.',
    )


def _build_cleanup_choices(cache):
    entries = []
    if cache.get('has_issues'):
        entries.append(('Quick Clean (recommended fast reset)', 'quick_clean'))
        entries.extend([
            ('Full Reset (clean everything)', 'full_reset'),
            ('Clean Stuck Resources (PVCs & PDBs only)', 'stuck_resources'),
            ('Clean Specific Release', 'specific_release'),
            ('Clean Completed Pods', 'completed_pods'),
            ('Verify Clean State', 'verify'),
            ('Dry Run (see what would be deleted)', 'dry_run'),
        ])
    else:
        entries.extend([
            ('Verify Clean State', 'verify'),
            ('Quick Clean (recommended fast reset)', 'quick_clean'),
            ('Full Reset (clean everything)', 'full_reset'),
            ('Clean Stuck Resources (PVCs & PDBs only)', 'stuck_resources'),
            ('Clean Specific Release', 'specific_release'),
            ('Clean Completed Pods', 'completed_pods'),
            ('Dry Run (see what would be deleted)', 'dry_run'),
        ])

    return [
        *[
            questionary.Choice(title=f'{index}) {label}', value=value)
            for index, (label, value) in enumerate(entries, start=1)
        ],
        questionary.Separator(),
        questionary.Choice(title='0) Back to Main Menu', value='back'),
    ]


def cleanup_menu():
    """Interactive cleanup menu."""
    clear_screen()
    cache = show_loading_spinner(ensure_cleanup_loaded, message="Loading cleanup state...")
    if cache is None:
        return

    drift_findings = run_drift_checks(cache.get('service_running'))
    drift_banner = get_drift_banner(drift_findings)
    if drift_banner:
        render_drift_banner(drift_banner, drift_findings)

    render_smart_cleanup(cache)
    choice = questionary.select(
        "Select cleanup option:",
        choices=_build_cleanup_choices(cache),
        style=custom_style
    ).ask()
    if not choice or choice == 'back':
        return
    handle_cleanup_choice(choice, cache.get('service_running', False))


def handle_cleanup_choice(choice, service_running):
    if not choice:
        return
    option_map = {
        'quick_clean': '1',
        'full_reset': '2',
        'stuck_resources': '3',
        'specific_release': '4',
        'completed_pods': '5',
        'verify': '6',
        'dry_run': '7',
    }
    option = option_map.get(choice, choice.split(')')[0] if isinstance(choice, str) and ')' in choice else choice)
    if option == '7':  # Dry Run
        dry_run = True
        action_choice = questionary.select(
            "Select dry run target:",
            choices=[
                "Quick Clean",
                "Full Reset",
                "Stuck Resources Only",
                "Specific Release",
            ]
        ).ask()
        if action_choice == "Quick Clean":
            option = '1'
        elif action_choice == "Full Reset":
            option = '2'
        elif action_choice == "Stuck Resources Only":
            option = '3'
        elif action_choice == "Specific Release":
            option = '4'
        else:
            return
    else:
        dry_run = False
    if option == '1':  # Quick Clean
        if dry_run:
            result = direct_quick_cleanup(dry_run=True)
            display_cleanup_result(result, dry_run=True)
        else:
            commands = get_quick_cleanup_commands()
            print("\nQuick clean will run:")
            for command in commands:
                print(f"   {' '.join(command)}")
            confirm = questionary.confirm(
                "Run quick clean now?",
                default=True
            ).ask()
            if not confirm:
                return
            print("\nRunning quick clean...")
            result = direct_quick_cleanup(dry_run=False)
            display_cleanup_result(result, dry_run=False)
    elif option == '2':  # Full Reset
        if not dry_run:
            confirm = questionary.confirm(
                "[WARN] This will delete ALL test resources. Continue?",
                default=False
            ).ask()
            if not confirm:
                return
        print("\nRunning full reset...")
        if service_running:
            result = call_service('/api/cleanup/reset', 'POST', {'dry_run': dry_run})
        else:
            result = direct_full_cleanup(dry_run)
        display_cleanup_result(result, dry_run)
    elif option == '3':  # Stuck Resources
        print("\nCleaning stuck resources (PVCs & PDBs)...")
        if service_running:
            result = call_service('/api/cleanup/stuck', 'POST', {'dry_run': dry_run})
        else:
            result = direct_stuck_cleanup(dry_run)
        display_cleanup_result(result, dry_run)
    elif option == '4':  # Specific Release
        releases = get_test_releases()
        if not releases:
            print("\nNo test releases found!")
        else:
            release = questionary.select(
                "Select release to clean:",
                choices=releases + ["Cancel"]
            ).ask()
            if release and release != "Cancel":
                if not dry_run:
                    confirm = questionary.confirm(
                        f"Delete release '{release}' and all its resources?",
                        default=False
                    ).ask()
                    if not confirm:
                        return
                print(f"\nCleaning release: {release}")
                if service_running:
                    result = call_service(
                        f'/api/cleanup/release/{release}',
                        'DELETE',
                        {'dry_run': dry_run}
                    )
                else:
                    result = direct_release_cleanup(release, dry_run)
                display_cleanup_result(result, dry_run)
    elif option == '5':  # Completed Pods
        print("\nCleaning completed pods...")
        if service_running:
            result = call_service('/api/cleanup/all', 'POST', {'dry_run': dry_run})
            if 'completed_pods' in result:
                result = result['completed_pods']
        else:
            result = direct_completed_pods_cleanup(dry_run)
        display_cleanup_result(result, dry_run)
    elif option == '6':  # Verify
        print("\nVerifying cluster state...")
        if service_running:
            result = call_service('/api/cleanup/verify')
        else:
            result = direct_verify_state()
        display_verification_result(result)
    _prompt_go_back()
