"""Live watch helpers for CLI test monitoring."""

import json
import subprocess
import time

from simulation_service_tool.cli.prompts import _prompt_go_back
from simulation_service_tool.services.api_client import call_service
from simulation_service_tool.services.command_runner import run_cli_command
from simulation_service_tool.ui.display import build_watch_renderable
from simulation_service_tool.ui.utils import clear_screen


_WATCH_RETRYABLE_ERRORS = (
    "tls handshake timeout",
    "context deadline exceeded",
    "client.timeout exceeded while awaiting headers",
    "i/o timeout",
    "connection refused",
    "service unavailable",
    "unable to connect to the server",
)


def _run_command(args, timeout=None):
    return run_cli_command(args, timeout=timeout)


def _kubectl_list_json(resource_type, label_selector=None):
    args = ["kubectl", "get", resource_type, "-o", "json"]
    if label_selector:
        args.extend(["-l", label_selector])

    result = _run_command(args)
    if result.returncode != 0 or not result.stdout.strip():
        return [], result.stderr.strip() or f"{resource_type} not found"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return [], f"Invalid kubectl JSON for {resource_type}: {exc}"
    return payload.get('items', []), None


def _get_recent_pod_logs(release_name=None, tail_lines=8):
    label = f"release={release_name}" if release_name else "app=playwright-agent"
    pods, error = _kubectl_list_json('pods', label)
    if error or not pods:
        return []

    log_entries = []
    for pod in pods:
        phase = ((pod.get('status') or {}).get('phase') or '').lower()
        if phase != 'running':
            continue
        pod_name = (pod.get('metadata') or {}).get('name', '')
        if not pod_name:
            continue
        result = _run_command(["kubectl", "logs", pod_name, f"--tail={tail_lines}"], timeout=2)
        if result.returncode == 0 and result.stdout and result.stdout.strip():
            lines = [line for line in result.stdout.strip().split('\n') if line.strip()][-tail_lines:]
            if lines:
                log_entries.append((pod_name, lines))
    return log_entries


def _watch_error_is_retryable(error_text):
    message = (error_text or '').lower()
    return any(marker in message for marker in _WATCH_RETRYABLE_ERRORS)


def _run_watch_command(release_name):
    return subprocess.run(
        ["kubectl", "get", "pods", "-w", "-l", f"release={release_name}"],
        check=False,
        text=True,
        stderr=subprocess.PIPE,
    )


def _print_watch_retry_notice(error_text, attempt, max_retries, retry_delay):
    detail = error_text or "kubectl exited unexpectedly."
    print(f"\n[WARN] Watch connection to the Kubernetes API failed: {detail}")
    print("       The Helm release was already installed, so the test may still be running.")
    print(f"       Retrying in {retry_delay}s (attempt {attempt + 1}/{max_retries}).")


def _print_watch_failure_guidance(release_name, error_text):
    detail = error_text or "kubectl exited unexpectedly."
    print("\n[WARN] Could not establish a stable watch connection to the Kubernetes API.")
    print(f"       Last error: {detail}")
    print("       The test may still be running. Check status manually with:")
    print(f"         kubectl get pods -l release={release_name}")


def watch_release_pods_kubectl(release_name, max_retries=3, retry_delay=5):
    """Watch pods for a specific release via kubectl -w."""
    print(f"\nWatching Kubernetes pods for release: {release_name}")
    print("(Press Ctrl+C to stop watching)\n")
    try:
        for attempt in range(1, max_retries + 1):
            result = _run_watch_command(release_name)
            error_text = (result.stderr or '').strip()

            if result.returncode == 0:
                break

            if _watch_error_is_retryable(error_text) and attempt < max_retries:
                _print_watch_retry_notice(error_text, attempt, max_retries, retry_delay)
                time.sleep(retry_delay)
                continue

            _print_watch_failure_guidance(release_name, error_text)
            break
    except KeyboardInterrupt:
        print("\nStopped watching pods.")
    _prompt_go_back()


def watch_agents(release_name=None, service_running=False):
    """Watch agent pods with live progress display."""
    poll_interval = 5
    stale_threshold = 90
    log_fetch_interval = 15

    last_completed = -1
    last_change_time = time.time()
    last_log_fetch = 0.0
    cached_pod_logs = []

    def _fetch_stats():
        if service_running:
            summary = call_service('/api/simulation/summary')
            return (
                summary.get('total', 0),
                summary.get('success', 0),
                summary.get('running', 0),
                summary.get('errors', 0),
                summary.get('pending', 0),
            )
        return get_pod_stats_direct(release_name)

    try:
        try:
            from rich.live import Live
            from simulation_service_tool.ui.display import console as rich_console, RICH_AVAILABLE

            if not RICH_AVAILABLE or rich_console is None:
                raise ImportError
        except ImportError:
            RICH_AVAILABLE = False

        if RICH_AVAILABLE:
            with Live(console=rich_console, refresh_per_second=1, screen=False) as live:
                while True:
                    total, success, running, failed, pending = _fetch_stats()
                    completed = success + failed

                    if completed != last_completed:
                        last_completed = completed
                        last_change_time = time.time()
                    stale_seconds = time.time() - last_change_time

                    now = time.time()
                    if running > 0 and now - last_log_fetch >= log_fetch_interval:
                        cached_pod_logs = _get_recent_pod_logs(release_name, tail_lines=4)
                        last_log_fetch = now
                    elif running == 0:
                        cached_pod_logs = []

                    renderable = build_watch_renderable(
                        release_name,
                        total,
                        success,
                        running,
                        failed,
                        pending,
                        pod_logs=cached_pod_logs,
                        stale_seconds=stale_seconds,
                    )
                    if renderable is not None:
                        live.update(renderable)

                    if total > 0 and completed == total:
                        time.sleep(1)
                        break
                    if stale_seconds > stale_threshold and running == 0 and pending == 0 and completed < total:
                        break

                    time.sleep(poll_interval)
        else:
            while True:
                clear_screen()
                total, success, running, failed, pending = _fetch_stats()
                completed = success + failed

                if completed != last_completed:
                    last_completed = completed
                    last_change_time = time.time()
                stale_seconds = time.time() - last_change_time

                print(f"\n  Watching: {release_name or 'all tests'}    (Ctrl+C to stop)\n")
                print("+" + "=" * 62 + "+")
                if total > 0:
                    progress = (completed / total) * 100
                    bar_length = 40
                    filled = int(bar_length * completed / total)
                    bar = '#' * filled + '-' * (bar_length - filled)
                    print(f"|  Progress: [{bar}] {progress:.1f}%{' ' * max(0, 5 - len(f'{progress:.1f}'))}|")
                    print(f"|  TOTAL: {total}  SUCCESS: {success}  RUNNING: {running}  FAILED: {failed}  PENDING: {pending}")
                else:
                    print("|  Waiting for agents to start...")
                print("+" + "=" * 62 + "+")

                if stale_seconds > stale_threshold and running == 0 and pending == 0:
                    print(f"\n  No progress for {int(stale_seconds)}s with 0 running pods. Exiting watch.")
                    break
                if total > 0 and completed == total:
                    break

                time.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\n\n  Stopped watching. Agents continue running in the cluster.")

    _prompt_go_back()


def get_pod_stats_direct(release_name=None):
    """Get pod stats directly via kubectl when the service is offline."""
    label_args = ["-l", f"release={release_name}"] if release_name else ["-l", "app=playwright-agent"]

    def count_pods(field_selector=None):
        cmd = ["kubectl", "get", "pods"] + label_args + ["--no-headers"]
        if field_selector:
            cmd += ["--field-selector", field_selector]
        result = subprocess.run(cmd, capture_output=True, text=True)
        lines = [line for line in result.stdout.strip().split('\n') if line.strip()] if result.stdout.strip() else []
        return len(lines)

    total = count_pods()
    running = count_pods("status.phase=Running")
    success = count_pods("status.phase=Succeeded")
    failed = count_pods("status.phase=Failed")
    pending = total - running - success - failed
    return total, success, running, failed, pending