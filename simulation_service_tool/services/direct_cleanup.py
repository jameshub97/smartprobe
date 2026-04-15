"""Direct cleanup functions (fallback when service not running)."""

import importlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from simulation_service_tool.services.command_runner import build_cli_command, format_command, run_cli_command

_service_module = None
_cleanup_instance = None


def _run_subprocess(args, shell=False):
    if shell:
        raise ValueError('shell=True is not allowed for direct cleanup commands')
    return run_cli_command(args)


def _get_service_module():
    global _service_module
    if _service_module is None:
        _service_module = importlib.import_module('simulation_service')
    return _service_module


def _get_cleanup_instance():
    global _cleanup_instance
    if _cleanup_instance is None:
        _cleanup_instance = _get_service_module().ClusterCleanup()
    return _cleanup_instance


def _shell_command(args):
    return format_command(build_cli_command(args))


def get_quick_cleanup_commands(releases=None):
    releases = releases if releases is not None else get_test_releases()
    commands = []

    for release in releases:
        commands.append(["helm", "uninstall", release, "--ignore-not-found"])

    commands.extend([
        ["kubectl", "delete", "pvc", "playwright-cache", "--ignore-not-found"],
        ["kubectl", "delete", "pdb", "playwright-agent-pdb", "--ignore-not-found"],
        ["kubectl", "delete", "statefulset", "playwright-agent", "--ignore-not-found"],
        [
            "kubectl", "delete", "pods",
            "-l", "app=playwright-agent",
            "--field-selector=status.phase=Failed",
            "--ignore-not-found",
        ],
    ])
    return commands


def direct_quick_cleanup(dry_run=False, releases=None):
    """Fast-path cleanup for stale releases and shared legacy resources."""
    releases = releases if releases is not None else get_test_releases()
    result = {
        'quick_cleanup': {
            'commands': [_shell_command(command) for command in get_quick_cleanup_commands(releases)],
            'resources': [],
        },
        'helm_releases': {'releases': []},
        'pods': [],
        'errors': [],
    }

    if dry_run:
        result['helm_releases']['releases'] = list(releases)
        result['quick_cleanup']['resources'] = [
            'pvc/playwright-cache',
            'pdb/playwright-agent-pdb',
            'statefulset/playwright-agent',
            'failed pods with label app=playwright-agent',
        ]
        return result

    for release in releases:
        cleanup_result = direct_release_cleanup(release, dry_run=False)
        if cleanup_result.get('error'):
            result['errors'].append(f"{release}: {cleanup_result['error']}")
            continue
        if cleanup_result.get('warning'):
            result['errors'].append(f"{release}: {cleanup_result['warning']}")
        if cleanup_result.get('helm') == 'uninstalled' or any(cleanup_result.get(key) for key in ('pods', 'pvcs', 'pdbs', 'jobs')):
            result['helm_releases']['releases'].append(release)

    resource_checks = [
        ("pvc", "playwright-cache"),
        ("pdb", "playwright-agent-pdb"),
        ("statefulset", "playwright-agent"),
    ]
    for kind, name in resource_checks:
        exists_result = _run_subprocess(["kubectl", "get", kind, name, "-o", "name"])
        if exists_result.returncode == 0 and exists_result.stdout.strip():
            _run_subprocess(["kubectl", "delete", kind, name, "--ignore-not-found"])
            result['quick_cleanup']['resources'].append(f"{kind}/{name}")

    failed_pods_result = _run_subprocess(
        [
            "kubectl", "get", "pods",
            "-l", "app=playwright-agent",
            "--field-selector=status.phase=Failed",
            "-o", "name",
        ]
    )
    failed_pods = [line.split('/', 1)[-1] for line in failed_pods_result.stdout.splitlines() if line.strip()]
    if failed_pods:
        _run_subprocess(
            [
                "kubectl", "delete", "pods",
                "-l", "app=playwright-agent",
                "--field-selector=status.phase=Failed",
                "--ignore-not-found",
            ]
        )
        result['pods'] = failed_pods

    return result


def get_test_releases():
    service_module = _get_service_module()
    return [release.get('name') for release in service_module.list_playwright_releases() if release.get('name')]


def direct_preflight_check():
    """Check for Kubernetes resource conflicts without relying on the Flask service."""
    conflicts = []

    pvc_result = _run_subprocess(["kubectl", "get", "pvc", "playwright-cache", "-o", "name"])
    if pvc_result.returncode == 0 and pvc_result.stdout.strip():
        conflicts.append({
            'type': 'pvc',
            'name': 'playwright-cache',
            'fix': 'kubectl delete pvc playwright-cache',
        })

    pdb_result = _run_subprocess(["kubectl", "get", "pdb", "playwright-agent-pdb", "-o", "name"])
    if pdb_result.returncode == 0 and pdb_result.stdout.strip():
        conflicts.append({
            'type': 'pdb',
            'name': 'playwright-agent-pdb',
            'fix': 'kubectl delete pdb playwright-agent-pdb',
            'note': 'Legacy shared PDB from older chart versions',
        })

    releases = get_test_releases()
    if releases:
        conflicts.append({
            'type': 'helm_releases',
            'releases': releases,
            'fix': 'helm uninstall <release-name>',
        })

    return {
        'has_conflicts': bool(conflicts),
        'conflicts': conflicts,
    }


def direct_full_cleanup(dry_run=False):
    """Use ClusterCleanup for full cleanup."""
    return _get_cleanup_instance().cleanup_all(dry_run=dry_run)


def direct_stuck_cleanup(dry_run=False):
    """Use ClusterCleanup for stuck resources cleanup."""
    return _get_cleanup_instance().cleanup_stuck_resources(dry_run=dry_run)


def direct_release_cleanup(release, dry_run=False):
    """Use ClusterCleanup for specific release cleanup."""
    service_module = _get_service_module()
    result = {'release': release, 'helm': None, 'pods': [], 'pvcs': [], 'pdbs': [], 'jobs': []}
    if not service_module.is_valid_release_name(release):
        result['error'] = f'Invalid release name: {release}'
        return result
    if not service_module.release_exists_or_has_resources(release):
        result['helm'] = 'not found'
        result['warning'] = f"No Helm release or owned resources found for '{release}'."
        return result
    if not dry_run:
        helm_result = _run_subprocess(["helm", "uninstall", release, "--ignore-not-found"])
        result['helm'] = 'uninstalled' if helm_result.returncode == 0 else helm_result.stderr.strip() or 'failed'
        for resource, singular, key in (("pods", "pod", 'pods'), ("pvc", "pvc", 'pvcs'), ("pdb", "pdb", 'pdbs'), ("jobs", "job", 'jobs')):
            names = service_module._list_release_owned_resource_names(resource, release)
            for name in names:
                _run_subprocess(["kubectl", "delete", singular, name, "--ignore-not-found"])
            result[key].extend(names)
    return result


def direct_completed_pods_cleanup(dry_run=False):
    """Use ClusterCleanup for completed pods cleanup."""
    return _get_cleanup_instance().cleanup_completed_pods(dry_run=dry_run)


def direct_verify_state():
    """Use ClusterCleanup to verify state.

    The k8s Python client can hang indefinitely on a stalled API server.
    We run it in a separate thread and abandon it after 8 seconds with
    shutdown(wait=False) — we do NOT use the context-manager form because
    that calls shutdown(wait=True), which blocks until the hung thread
    finishes and defeats the timeout entirely.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    # Move _get_cleanup_instance() inside the lambda so k8s init also
    # happens in the executor thread and is subject to the timeout.
    future = pool.submit(lambda: _get_cleanup_instance().cleanup_all(dry_run=True))
    dry_run_result = None
    try:
        dry_run_result = future.result(timeout=8)
    except Exception:
        pass
    finally:
        pool.shutdown(wait=False)

    if dry_run_result is not None:
        state = {
            'helm_test_releases': len(dry_run_result.get('helm_releases', {}).get('releases', [])),
            'playwright_pods': 0,
            'playwright_pvcs': len(dry_run_result.get('orphaned_pvcs', {}).get('pvcs', [])),
            'conflicting_pdbs': len(dry_run_result.get('conflicting_pdbs', {}).get('pdbs', [])),
        }
        state['is_clean'] = (
            state['helm_test_releases'] == 0 and
            state['playwright_pods'] == 0 and
            state['playwright_pvcs'] == 0 and
            state['conflicting_pdbs'] == 0
        )
        return state

    # Fallback: subprocess-based checks (each has COMMAND_TIMEOUTS budget).
    state = {
        'helm_test_releases': 0,
        'playwright_pods': 0,
        'playwright_pvcs': 0,
        'conflicting_pdbs': 0,
    }
    releases = get_test_releases()
    state['helm_test_releases'] = len(releases)
    result = _run_subprocess(["kubectl", "get", "pods", "-l", "app=playwright-agent", "-o", "name"])
    state['playwright_pods'] = len([line for line in result.stdout.splitlines() if line.strip()])
    result = _run_subprocess(["kubectl", "get", "pvc", "playwright-cache", "-o", "name"])
    state['playwright_pvcs'] = 1 if result.returncode == 0 and result.stdout.strip() else 0
    result = _run_subprocess(["kubectl", "get", "pdb", "playwright-agent-pdb", "-o", "name"])
    state['conflicting_pdbs'] = 1 if result.returncode == 0 and result.stdout.strip() else 0
    state['is_clean'] = (
        state['helm_test_releases'] == 0 and
        state['playwright_pods'] == 0 and
        state['playwright_pvcs'] == 0 and
        state['conflicting_pdbs'] == 0
    )
    return state
