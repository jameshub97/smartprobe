"""Routine health checks menu."""

import questionary

from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen
from simulation_service_tool.ui.display import render_routine_checks_dashboard, render_drift_banner, render_smart_summary_panel
from simulation_service_tool.services.smart_diagnostics import run_drift_checks, get_drift_banner
from simulation_service_tool.cli.commands import (
    diagnose_unhealthy_pod,
    get_routine_checks_snapshot,
    preflight_check,
    show_stale_pod_summary,
)
from simulation_service_tool.menus.cleanup import cleanup_menu
from simulation_service_tool.menus.ports import ports_menu


def _routine_issue(summary, remediation):
    return {
        'summary': summary,
        'remediation': remediation,
    }


def _build_routine_issues(snapshot):
    issues = []
    unhealthy_pods = snapshot.get('unhealthy_pods', [])
    conflicts = snapshot.get('preflight_conflicts', [])
    stale_info = snapshot.get('stale_pod') or {}

    if snapshot.get('pods_pending'):
        issues.append(_routine_issue(
            'pod status has not been loaded yet',
            'Use Refresh to run the full Kubernetes pod scan before diagnosing test agents.',
        ))
    elif unhealthy_pods:
        issues.append(_routine_issue(
            f"{len(unhealthy_pods)} unhealthy pod(s) detected",
            'Use Diagnose unhealthy pod, unless the pod is stale, in which case inspect the stale pod first.',
        ))

    if snapshot.get('preflight_pending'):
        issues.append(_routine_issue(
            'preflight conflict scan has not been loaded yet',
            'Run preflight cleanup to detect orphaned Helm, PVC, or PDB conflicts.',
        ))
    elif conflicts:
        issues.append(_routine_issue(
            f"{len(conflicts)} orphaned resource conflict(s) detected",
            'Open Cleanup Center or run preflight cleanup again after remediation to confirm the cluster is clear.',
        ))

    if snapshot.get('stale_pending'):
        issues.append(_routine_issue(
            'stale StatefulSet inspection has not been loaded yet',
            'Use Refresh if you need stale-revision checks before diagnosing a crashing pod.',
        ))
    elif stale_info.get('is_stale'):
        issues.append(_routine_issue(
            f"stale pod detected: {stale_info.get('pod_name', 'playwright-agent-0')}",
            'Inspect the stale pod first, then recreate it or use Cleanup Center before resuming diagnosis.',
        ))

    return issues


def _render_smart_routine_summary(snapshot, drift_findings=None):
    issues = _build_routine_issues(snapshot)
    if drift_findings:
        for finding in reversed(drift_findings):
            if finding['severity'] in ('warning', 'error'):
                issues.insert(0, _routine_issue(finding['summary'], finding['remediation']))
    render_smart_summary_panel(
        'Routine Check Actions',
        issues=issues,
        recommendation='Recommended: follow the first remediation before entering test operations.',
        healthy_message='Routine checks look healthy. Continue to Test Operations when ready.',
    )


def _build_routine_check_choices(snapshot):
    stale_info = snapshot.get('stale_pod') or {}
    if stale_info.get('is_stale'):
        primary_choice = questionary.Choice(
            title='1) Inspect stale pod (diagnosis disabled while stale revision exists)',
            value='inspect_stale',
        )
    else:
        primary_choice = questionary.Choice(title='1) Diagnose unhealthy pod', value='diagnose_unhealthy')

    return [
        primary_choice,
        questionary.Choice(title='2) Open Cleanup Center', value='cleanup'),
        questionary.Choice(title='3) Run preflight cleanup', value='preflight'),
        questionary.Choice(title='4) Manage ports', value='ports'),
        questionary.Choice(title='5) Refresh', value='refresh'),
        questionary.Separator(),
        questionary.Choice(title='0) Back to dashboard', value='back'),
    ]


def _apply_cached_preflight_result(snapshot, cached_preflight):
    if not cached_preflight or cached_preflight.get('cancelled'):
        return snapshot

    updated_snapshot = dict(snapshot)
    conflicts = cached_preflight.get('conflicts', [])
    updated_snapshot['preflight_conflicts'] = conflicts
    updated_snapshot['preflight_pending'] = False
    return updated_snapshot


def routine_checks_menu(service_running):
    detailed_scan = False
    cached_preflight = None
    drift_findings = None
    while True:
        clear_screen()
        snapshot = get_routine_checks_snapshot(
            service_running,
            include_preflight=False,
            include_stale=detailed_scan,
            include_pods=detailed_scan,
        )
        snapshot = _apply_cached_preflight_result(snapshot, cached_preflight)

        if drift_findings is None:
            drift_findings = run_drift_checks(service_running)

        render_routine_checks_dashboard(snapshot)

        drift_banner = get_drift_banner(drift_findings)
        if drift_banner:
            render_drift_banner(drift_banner, drift_findings)

        _render_smart_routine_summary(snapshot, drift_findings)

        choice = questionary.select(
            "What would you like to do?",
            choices=_build_routine_check_choices(snapshot),
            style=custom_style,
        ).ask()

        if not choice or choice == 'back':
            return
        if choice == 'inspect_stale':
            show_stale_pod_summary(service_running)
        elif choice == 'diagnose_unhealthy':
            diagnose_unhealthy_pod(service_running)
        elif choice == 'cleanup':
            cleanup_menu()
        elif choice == 'preflight':
            result = preflight_check(service_running)
            if result is not None and not result.get('cancelled'):
                cached_preflight = result
        elif choice == 'ports':
            ports_menu()
        elif choice == 'refresh':
            detailed_scan = True
            drift_findings = None
            continue