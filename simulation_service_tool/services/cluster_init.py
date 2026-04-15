"""One-time cluster initialization for a known-good state."""

import os
import tempfile
import time

from simulation_service_tool.services.command_runner import run_cli_command
from simulation_service_tool.services.direct_cleanup import (
    direct_full_cleanup,
    direct_completed_pods_cleanup,
    direct_verify_state,
    get_test_releases,
)
from simulation_service_tool.services.hung_api_cleanup import clear_hung_api_listeners

_INIT_FLAG_PATH = os.path.join(tempfile.gettempdir(), '.simulation_cluster_initialized')

# Flag expires after 4 hours (cluster may drift)
_INIT_TTL_SECONDS = 4 * 60 * 60


def is_initialized():
    """Check if cluster has been initialized this session."""
    if not os.path.exists(_INIT_FLAG_PATH):
        return False
    try:
        age = time.time() - os.path.getmtime(_INIT_FLAG_PATH)
        if age > _INIT_TTL_SECONDS:
            os.remove(_INIT_FLAG_PATH)
            return False
        return True
    except OSError:
        return False


def set_initialized():
    """Mark cluster as initialized."""
    try:
        with open(_INIT_FLAG_PATH, 'w') as f:
            f.write(str(time.time()))
    except OSError:
        pass


def clear_initialized():
    """Clear the initialization flag."""
    try:
        os.remove(_INIT_FLAG_PATH)
    except OSError:
        pass


def _step_clear_hung_api_listeners():
    """Release hung local API listeners before cluster initialization.

    If a stale simulation-service listener was cleared, restart it immediately
    so init continues against a fresh local API process.
    """
    return clear_hung_api_listeners(restart_simulation=True)


def _step_load_kubeconfig():
    """Verify kubeconfig is accessible and the API server is reachable."""
    result = run_cli_command(["kubectl", "get", "nodes", "-o", "name", "--request-timeout=5s"], timeout=8)
    return {
        'success': result.returncode == 0,
        'detail': f"{result.stdout.count('node/')} node(s) ready" if result.returncode == 0 else (result.stderr.strip().splitlines()[0] if result.stderr.strip() else 'kubectl get nodes failed'),
    }


def _step_update_helm_repos():
    """Update Helm repos (best-effort)."""
    result = run_cli_command(["helm", "repo", "update"], timeout=15)
    # Not fatal if there are no repos configured
    return {
        'success': True,
        'detail': 'updated' if result.returncode == 0 else 'skipped (no repos or offline)',
    }


def _step_clean_orphaned_resources():
    """Remove all orphaned test resources."""
    # Get releases from both helm list and the service module
    releases = get_test_releases()

    # Also catch releases that helm list might report directly
    helm_list = run_cli_command(["helm", "list", "--short", "-q"], timeout=5)
    if helm_list.returncode == 0:
        for name in helm_list.stdout.strip().splitlines():
            name = name.strip()
            if name and name not in releases:
                releases.append(name)

    for release in releases:
        run_cli_command(["helm", "uninstall", release, "--ignore-not-found"], timeout=10)

    direct_full_cleanup(dry_run=False)
    direct_completed_pods_cleanup(dry_run=False)

    cleanup_commands = [
        ["kubectl", "delete", "pvc", "playwright-cache", "--ignore-not-found"],
        ["kubectl", "delete", "pdb", "playwright-agent-pdb", "--ignore-not-found"],
        ["kubectl", "delete", "statefulset", "playwright-agent", "--ignore-not-found"],
        ["kubectl", "delete", "jobs", "-l", "app=playwright-agent", "--ignore-not-found"],
        ["kubectl", "delete", "pods", "-l", "app=playwright-agent", "--force", "--grace-period=0", "--ignore-not-found"],
    ]
    for cmd in cleanup_commands:
        run_cli_command(cmd, timeout=10)

    # Verify cleanup actually worked — retry stragglers once
    remaining = get_test_releases()
    for release in remaining:
        run_cli_command(["helm", "uninstall", release, "--ignore-not-found"], timeout=10)

    total_cleaned = len(releases) + len(remaining)
    return {'success': True, 'detail': f'{total_cleaned} releases cleaned'}


def _step_verify_cluster_health():
    """Verify cluster is in a clean state."""
    state = direct_verify_state()
    return {
        'success': state.get('is_clean', False),
        'detail': 'clean' if state.get('is_clean') else (
            f"remaining: releases={state.get('helm_test_releases', 0)}, "
            f"pods={state.get('playwright_pods', 0)}, "
            f"pvcs={state.get('playwright_pvcs', 0)}, "
            f"pdbs={state.get('conflicting_pdbs', 0)}"
        ),
    }


INIT_STEPS = [
    ("Clearing hung API listeners", _step_clear_hung_api_listeners),
    ("Loading kubeconfig", _step_load_kubeconfig),
    ("Updating Helm repos", _step_update_helm_repos),
    ("Cleaning orphaned resources", _step_clean_orphaned_resources),
    ("Verifying cluster health", _step_verify_cluster_health),
]


def initialize_cluster(progress_callback=None):
    """Run all initialization steps. Returns (success, results)."""
    progress = progress_callback or (lambda _msg: None)
    results = []

    for name, fn in INIT_STEPS:
        progress(f"{name}...")
        try:
            result = fn()
        except Exception as e:
            result = {'success': False, 'detail': str(e)}
        results.append((name, result))

        if not result['success'] and name == "Loading kubeconfig":
            # Kubeconfig is fatal — can't proceed without it
            return False, results

    all_passed = all(r['success'] for _, r in results)
    if all_passed:
        set_initialized()

    return all_passed, results
