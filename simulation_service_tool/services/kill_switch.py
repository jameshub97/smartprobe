"""Emergency kill switch — force-delete all active pods and Helm releases."""

from __future__ import annotations

from simulation_service_tool.services.command_runner import run_cli_command


DEFAULT_NAMESPACE = "default"


def list_helm_releases(namespace: str = DEFAULT_NAMESPACE) -> list[str]:
    """List Helm releases in *namespace*."""
    result = run_cli_command(
        ["helm", "list", "--short", "-q"],
        namespace=namespace,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return [
        release.strip()
        for release in result.stdout.strip().splitlines()
        if release.strip()
    ]


def get_active_pods(namespace: str = DEFAULT_NAMESPACE) -> list[dict]:
    """List all non-completed pods in *namespace*.

    Returns a list of ``{'name': str, 'status': str, 'ready': str}`` dicts.
    """
    result = run_cli_command(
        ["kubectl", "get", "pods", "--no-headers"],
        namespace=namespace,
    )
    pods: list[dict] = []
    if result.returncode != 0 or not result.stdout.strip():
        return pods
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            pods.append({
                "name": parts[0],
                "ready": parts[1],
                "status": parts[2],
            })
    return pods


def probe_kill_switch_targets(
    namespace: str = DEFAULT_NAMESPACE,
    progress_callback=None,
) -> dict:
    """Inspect kill-switch targets before prompting the user."""
    progress = progress_callback or (lambda _message: None)
    progress(f"Checking active pods in {namespace}...")
    pods = get_active_pods(namespace)
    progress(f"Checking Helm releases in {namespace}...")
    releases = list_helm_releases(namespace)
    progress("Kill switch probe complete.")
    return {
        "namespace": namespace,
        "pods": pods,
        "releases": releases,
        "pod_count": len(pods),
        "release_count": len(releases),
        "has_targets": bool(pods or releases),
    }


def kill_all_pods(namespace: str = DEFAULT_NAMESPACE) -> dict:
    """Force-delete every pod in *namespace*.

    Returns ``{'success': bool, 'deleted': int, 'errors': list[str]}``.
    """
    pods = get_active_pods(namespace)
    if not pods:
        return {"success": True, "deleted": 0, "errors": []}

    result = run_cli_command(
        ["kubectl", "delete", "pods", "--all",
         "--force", "--grace-period=0"],
        namespace=namespace,
        timeout=30,
    )
    if result.returncode == 0:
        return {"success": True, "deleted": len(pods), "errors": []}
    return {
        "success": False,
        "deleted": 0,
        "errors": [(result.stderr or result.stdout or "unknown error").strip()],
    }


def kill_simulation_pods(namespace: str = DEFAULT_NAMESPACE) -> dict:
    """Force-delete only ``app=playwright-agent`` pods.

    Returns ``{'success': bool, 'deleted': int, 'errors': list[str]}``.
    """
    result = run_cli_command(
        ["kubectl", "get", "pods",
         "-l", "app=playwright-agent",
         "--no-headers"],
        namespace=namespace,
    )
    count = len([l for l in (result.stdout or "").strip().splitlines() if l.strip()])
    if count == 0:
        return {"success": True, "deleted": 0, "errors": []}

    del_result = run_cli_command(
        ["kubectl", "delete", "pods",
         "-l", "app=playwright-agent",
         "--force", "--grace-period=0"],
        namespace=namespace,
        timeout=30,
    )
    if del_result.returncode == 0:
        return {"success": True, "deleted": count, "errors": []}
    return {
        "success": False,
        "deleted": 0,
        "errors": [(del_result.stderr or del_result.stdout or "unknown error").strip()],
    }


def nuke_all(namespace: str = DEFAULT_NAMESPACE) -> dict:
    """Nuclear option — uninstall all Helm releases then force-delete all pods.

    Returns ``{'releases_removed': int, 'pods_deleted': int,
               'errors': list[str]}``.
    """
    errors: list[str] = []

    # Step 1: get all Helm releases in namespace
    releases = list_helm_releases(namespace)

    releases_removed = 0
    for release in releases:
        r = run_cli_command(
            ["helm", "uninstall", release, "--ignore-not-found"],
            namespace=namespace,
            timeout=15,
        )
        if r.returncode == 0:
            releases_removed += 1
        else:
            errors.append(f"helm uninstall {release}: {(r.stderr or '').strip()}")

    # Step 2: force-delete remaining pods
    pod_result = kill_all_pods(namespace)
    errors.extend(pod_result.get("errors", []))

    return {
        "releases_removed": releases_removed,
        "pods_deleted": pod_result.get("deleted", 0),
        "errors": errors,
    }
