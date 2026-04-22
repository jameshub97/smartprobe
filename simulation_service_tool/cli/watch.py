"""Live watch helpers for CLI test monitoring."""

import subprocess
import time

from simulation_service_tool.cli.prompts import _prompt_go_back


_WATCH_RETRYABLE_ERRORS = (
    "tls handshake timeout",
    "context deadline exceeded",
    "client.timeout exceeded while awaiting headers",
    "i/o timeout",
    "connection refused",
    "service unavailable",
    "unable to connect to the server",
)


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
    print(f"\n[33m[WARN][0m Watch connection to the Kubernetes API failed: {detail}")
    print("       The Helm release was already installed, so the test may still be running.")
    print(f"       Retrying in {retry_delay}s (attempt {attempt + 1}/{max_retries}).")


def _print_watch_failure_guidance(release_name, error_text):
    detail = error_text or "kubectl exited unexpectedly."
    print("\n[33m[WARN][0m Could not establish a stable watch connection to the Kubernetes API.")
    print(f"       Last error: {detail}")
    print("       The test may still be running. Check status manually with:")
    print(f"         kubectl get pods -l release={release_name}")


def watch_release_pods_kubectl(release_name, max_retries=3, retry_delay=5):
    """Watch pods for a specific release via kubectl -w."""
    import shutil
    if not shutil.which('kubectl'):
        print("\n[33m[WARN][0m kubectl not found in PATH — cannot watch pods live.")
        print(f"       Check status manually with:")
        print(f"         kubectl get pods -l release={release_name}")
        _prompt_go_back("Return to main menu")
        return

    print(f"\nWatching Kubernetes pods for release: {release_name}")
    print("(Press Ctrl+C to return to main menu)\n")
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
    _prompt_go_back("Return to main menu")


