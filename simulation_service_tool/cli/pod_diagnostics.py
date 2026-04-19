"""Interactive pod diagnostics and inspection flows."""

import subprocess

import questionary

from simulation_service_tool.cli.prompts import _prompt_go_back
from simulation_service_tool.cli.snapshots import (
    _collect_release_pod_assessment,
    _extract_waiting_reason,
    _format_pod_age,
    _get_statefulset_stale_status,
    _kubectl_list_json,
    _pod_ready_value,
    _pod_restart_count,
    _pod_status_value,
)
from simulation_service_tool.cli.workload_guidance import (
    _show_job_yaml_guidance,
    _show_statefulset_keepalive_guidance,
)
from simulation_service_tool.menus.ports import get_port_status, print_port_status_report
from simulation_service_tool.services.command_runner import run_cli_command
from simulation_service_tool.services.direct_cleanup import direct_verify_state
from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen


def _run_command(args, timeout=None):
    return run_cli_command(args, timeout=timeout)


def _pick_debug_pod(pods):
    def pod_rank(pod):
        status = pod.get('status', {}) or {}
        container_statuses = status.get('containerStatuses', []) or []
        waiting_reasons = []
        restart_count = 0
        for container_status in container_statuses:
            restart_count += container_status.get('restartCount', 0)
            waiting = (container_status.get('state', {}) or {}).get('waiting') or {}
            reason = waiting.get('reason')
            if reason:
                waiting_reasons.append(reason)

        phase = status.get('phase', 'Unknown')
        if waiting_reasons:
            return (0, -restart_count)
        if restart_count > 0:
            return (1, -restart_count)
        if phase not in {'Succeeded', 'Completed', 'Running'}:
            return (2, 0)
        if phase == 'Running':
            return (3, 0)
        return (4, 0)

    if not pods:
        return None
    ranked = sorted(pods, key=pod_rank)
    return ranked[0]


def _get_pod_logs_output(pod_name):
    result = _run_command(["kubectl", "logs", pod_name, "--tail=120"], timeout=2)
    output = result.stdout or result.stderr
    if output.strip() and result.returncode == 0:
        return output

    previous_result = _run_command(["kubectl", "logs", pod_name, "--previous", "--tail=120"], timeout=2)
    previous_output = previous_result.stdout or previous_result.stderr
    if previous_output.strip():
        return previous_output

    return output


def _get_owner_kind(pod):
    owner_references = ((pod.get('metadata', {}) or {}).get('ownerReferences', []) or [])
    if not owner_references:
        return None
    return owner_references[0].get('kind')


def _get_owner_name(pod):
    owner_references = ((pod.get('metadata', {}) or {}).get('ownerReferences', []) or [])
    if not owner_references:
        return None
    return owner_references[0].get('name')


def _get_stale_status_for_pod(pod):
    if _get_owner_kind(pod) != 'StatefulSet':
        return None

    pod_name = (pod.get('metadata', {}) or {}).get('name')
    statefulset_name = _get_owner_name(pod) or 'playwright-agent'
    if not pod_name:
        return None

    return _get_statefulset_stale_status(pod_name=pod_name, statefulset_name=statefulset_name)


def _print_stale_pod_details(stale_info):
    print(f"\n  Pod: {stale_info['pod_name']}")
    print(f"  Pod revision: {stale_info['pod_revision']}")
    print(f"  Current revision: {stale_info['current_revision']}")
    print(f"  Created: {stale_info.get('pod_created') or 'unknown'}")
    print(f"  Waiting reason: {stale_info.get('waiting_reason') or 'none'}")

    if stale_info.get('is_stale'):
        print("\n  [33m[WARN][0m This pod is stale and is not running the latest StatefulSet revision.")
        if stale_info.get('is_crashing'):
            print("  [36m[INFO][0m It is also failing, so recreating it is likely the right fix.")
        else:
            print("  [36m[INFO][0m It is stale but not currently crashing.")
    else:
        print("\n  [32m[OK][0m The StatefulSet pod is running the current revision.")


def _detect_statefulset_test_workload_mismatch(pod, logs_output):
    owner_kind = _get_owner_kind(pod)
    if owner_kind != 'StatefulSet':
        return None

    lowered = (logs_output or '').lower()
    shard_started = 'running shard' in lowered
    lifecycle_markers = [
        'npm notice',
        'passed',
        'failed',
        'error:',
    ]
    likely_test_execution = shard_started and any(marker in lowered for marker in lifecycle_markers)
    if not likely_test_execution:
        return None

    return {
        'kind': 'statefulset_test_workload',
        'controller': owner_kind,
        'summary': 'A StatefulSet pod is running a one-time test workload and then exiting.',
        'problem': 'StatefulSet pods are restarted when the container exits, so completed test runs show up as crash loops.',
        'fix': 'Use a Kubernetes Job for test agents, or keep the container alive intentionally if a long-running StatefulSet is required.',
    }


def _extract_release_name_from_pod(pod):
    labels = ((pod.get('metadata', {}) or {}).get('labels', {}) or {})
    return labels.get('release') or labels.get('app.kubernetes.io/instance') or 'playwright-agent'


def diagnose_unhealthy_pod(service_running):
    clear_screen()
    print("+" + "=" * 62 + "+")
    print("|                  UNHEALTHY POD DIAGNOSIS                     |")
    print("+" + "=" * 62 + "+")

    print("\n  [36m[INFO][0m Scanning agent pods...")

    pods, pod_error = _kubectl_list_json('pods', 'app=playwright-agent')
    if pod_error:
        print(f"\n  [33m[WARN][0m {pod_error}")
        _prompt_go_back()
        return

    print("  [36m[INFO][0m Selecting the most relevant unhealthy pod...")
    pod = _pick_debug_pod(pods)
    if not pod:
        print("\n  [32m[OK][0m No unhealthy pod found.")
        _prompt_go_back()
        return

    pod_name = (pod.get('metadata', {}) or {}).get('name', 'unknown')
    release_name = _extract_release_name_from_pod(pod)
    print(f"\n  Pod: {pod_name}")
    print(f"  Ready: {_pod_ready_value(pod)}")
    print(f"  Status: {_pod_status_value(pod)}")
    print(f"  Restarts: {_pod_restart_count(pod)}")
    print(f"  Age: {_format_pod_age((pod.get('metadata', {}) or {}).get('creationTimestamp'))}")
    owner_kind = _get_owner_kind(pod) or 'Unknown'
    print(f"  Controller: {owner_kind}")

    stale_info = _get_stale_status_for_pod(pod)
    if stale_info and stale_info.get('is_stale'):
        print("\n  [33m[WARN][0m Selected pod belongs to a stale StatefulSet revision.")
        _print_stale_pod_details(stale_info)
        print("\n  [ACTION]")
        print("  Diagnosis is paused because stale revisions can make pod logs misleading.")
        print("  Open Cleanup Center or recreate the StatefulSet pod before running unhealthy pod diagnosis.")
        _prompt_go_back('Return to routine checks')
        return

    print("\n  [36m[INFO][0m Fetching recent logs (tail 120, 2s timeout per attempt)...")
    logs_output = _get_pod_logs_output(pod_name)
    log_lines = [line.rstrip() for line in logs_output.splitlines() if line.strip()]
    if log_lines:
        print("\n  Recent logs:")
        for line in log_lines[-12:]:
            print(f"    {line}")
    else:
        print("\n  [36m[INFO][0m No recent logs available.")

    shard_fix_applied = 'running shard 1/' in logs_output.lower() or 'running shard ' in logs_output.lower()
    mismatch = _detect_statefulset_test_workload_mismatch(pod, logs_output)
    if mismatch:
        print("\n  [ANALYSIS]")
        print(f"  {'[32m[OK][0m' if shard_fix_applied else '[36m[INFO][0m'} Shard fix applied: {'yes' if shard_fix_applied else 'not confirmed'}")
        print("  [33m[WARN][0m Pod is still crashing after the shard fix.")
        print("\n  [ROOT CAUSE]")
        print(f"  {mismatch['summary']}")
        print(f"  {mismatch['problem']}")
        print("\n  [SOLUTION]")
        print("  Option 1: Use a Kubernetes Job for test agents (recommended)")
        print("  Option 2: Keep the StatefulSet alive intentionally after the test")

        action = questionary.select(
            'What would you like to do?',
            choices=[
                questionary.Choice(title='Show me the Job YAML', value='show_job_yaml'),
                questionary.Choice(title='Show StatefulSet keep-alive workaround', value='show_keepalive'),
                questionary.Choice(title='Ignore; this restart pattern is expected', value='ignore'),
                questionary.Choice(title='Go back', value='back'),
            ],
            style=custom_style,
        ).ask()

        if action == 'show_job_yaml':
            _show_job_yaml_guidance(release_name)
            return
        if action == 'show_keepalive':
            _show_statefulset_keepalive_guidance()
            return

    _prompt_go_back()


def show_active_pods_summary(service_running):
    clear_screen()
    print("+" + "=" * 62 + "+")
    print("|                     ACTIVE POD CHECK                         |")
    print("+" + "=" * 62 + "+")

    pod_assessment = _collect_release_pod_assessment()
    if pod_assessment.get('error'):
        print(f"\n  [33m[WARN][0m {pod_assessment['error']}")
        _prompt_go_back()
        return

    print(f"\n  Total agent pods: {pod_assessment.get('total', 0)}")
    print(f"  Healthy pods: {pod_assessment.get('healthy', 0)}")
    waiting_reasons = pod_assessment.get('waiting_reasons', [])
    if waiting_reasons:
        print(f"  Waiting reasons: {', '.join(waiting_reasons)}")

    if not service_running:
        direct_state = direct_verify_state()
        print(f"\n  Test releases: {direct_state.get('helm_test_releases', '?')}")
        print(f"  Cluster clean: {'yes' if direct_state.get('is_clean') else 'no'}")

    _prompt_go_back()


def show_stale_pod_summary(service_running):
    clear_screen()
    print("+" + "=" * 62 + "+")
    print("|                  STALE POD INSPECTION                        |")
    print("+" + "=" * 62 + "+")

    stale_info = _get_statefulset_stale_status()
    if not stale_info:
        print("\n  [36m[INFO][0m No StatefulSet pod named 'playwright-agent-0' is currently present.")
        _prompt_go_back()
        return

    _print_stale_pod_details(stale_info)

    _prompt_go_back()


def show_active_ports_summary():
    clear_screen()
    print_port_status_report(get_port_status())
    _prompt_go_back()


def view_agent_logs():
    """Interactive log viewer for playwright-agent pods."""
    clear_screen()
    print("+" + "=" * 62 + "+")
    print("|                    AGENT POD LOGS                            |")
    print("+" + "=" * 62 + "+")

    pods, pod_error = _kubectl_list_json('pods', 'app=playwright-agent')
    if pod_error:
        print(f"\n  \033[33m[WARN]\033[0m {pod_error}")
        _prompt_go_back()
        return

    if not pods:
        print("\n  \033[36m[INFO]\033[0m No agent pods found.")
        _prompt_go_back()
        return

    pod_choices = []
    for pod in pods:
        name = (pod.get('metadata', {}) or {}).get('name', 'unknown')
        status = _pod_status_value(pod)
        restarts = _pod_restart_count(pod)
        age = _format_pod_age((pod.get('metadata', {}) or {}).get('creationTimestamp'))
        label = f"{name}  [{status}  restarts={restarts}  age={age}]"
        pod_choices.append(questionary.Choice(title=label, value=name))

    pod_choices.append(questionary.Separator())
    pod_choices.append(questionary.Choice(title="Back", value="__back__"))

    try:
        pod_name = questionary.select(
            "Select a pod to view logs:",
            choices=pod_choices,
            style=custom_style,
        ).ask()
    except KeyboardInterrupt:
        return

    if not pod_name or pod_name == "__back__":
        return

    print(f"\n  \033[36m[INFO]\033[0m Fetching logs for {pod_name}...")
    logs_output = _get_pod_logs_output(pod_name)
    log_lines = [line.rstrip() for line in logs_output.splitlines() if line.strip()]

    if not log_lines:
        print("\n  \033[36m[INFO]\033[0m No logs available for this pod.")
        _prompt_go_back()
        return

    print(f"\n  Showing last {min(len(log_lines), 100)} of {len(log_lines)} lines:\n")
    for line in log_lines[-100:]:
        print(f"  {line}")

    try:
        action = questionary.select(
            "Next:",
            choices=[
                questionary.Choice(title="Stream live  (kubectl logs -f)", value="stream"),
                questionary.Choice(title="Back", value="back"),
            ],
            style=custom_style,
        ).ask()
    except KeyboardInterrupt:
        return

    if action == "stream":
        print(f"\n  Streaming logs for {pod_name}  (Ctrl+C to stop)...\n")
        try:
            subprocess.run(["kubectl", "logs", "-f", pod_name], check=False)
        except KeyboardInterrupt:
            print("\n  Stopped streaming.")
        _prompt_go_back()
