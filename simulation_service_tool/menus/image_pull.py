"""Image Pull Debugger — scans for ErrImagePull / ImagePullBackOff pods
and guides the user through diagnosis and fixes.

Menu flow
---------
1. Scan all pods for image pull errors (``scan_image_pull_errors()``)
2. For each failing pod, describe it and extract the root cause
3. Render a diagnosis box (``render_image_pull_diagnosis()``)
4. Offer targeted fixes via questionary select loop

Fixes offered
-------------
- Check local registry reachability (TCP probe to host.docker.internal:5050)
- Show the ``docker push`` commands needed to repopulate the registry
- Show the DaemonSet YAML that patches containerd's registry mirrors
- Patch pod's owner deployment/job to use ``imagePullPolicy: IfNotPresent``
- Re-run diagnosis
- Back (exit to main menu)
"""

import re
import socket
import subprocess
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed

import questionary

from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen
from simulation_service_tool.services.command_runner import run_cli_command
from simulation_service_tool.cli.prompts import _prompt_continue


# ── helpers ───────────────────────────────────────────────────────────────────

_IMAGE_PULL_STATUSES = {"ErrImagePull", "ImagePullBackOff"}
_IMAGE_RE = re.compile(r'image\s+"([^"]+)"', re.IGNORECASE)
_FAILED_RE = re.compile(
    r'(failed to pull image|back-off pulling image|error response from daemon)',
    re.IGNORECASE,
)


def scan_image_pull_errors(namespace: str = "default", max_describe: int = 50) -> list[dict]:
    """Return a list of pods that have image pull errors.

    Describes up to *max_describe* failing pods in parallel (default 50).
    Once the image name is known from one pod, subsequent pods with the same
    name prefix reuse it without an extra describe call.

    Each entry::

        {
            "name":       str,          # pod name
            "namespace":  str,
            "status":     str,          # ErrImagePull | ImagePullBackOff | …
            "image":      str | None,   # first bad image found in events
            "message":    str,          # raw describe excerpt (first 3 failing event lines)
        }
    """
    result = run_cli_command(
        ["kubectl", "get", "pods",
         "--no-headers",
         "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.phase,READY:.status.containerStatuses[*].state.waiting.reason"],
        namespace=namespace,
    )
    if result.returncode != 0 or not result.stdout.strip():
        # Fall back: get pods wide and parse the STATUS column
        result = run_cli_command(
            ["kubectl", "get", "pods", "--no-headers"],
            namespace=namespace,
        )

    # Collect failing pod names + matched status from the raw text
    candidates: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pod_name = parts[0]
        line_upper = line.upper()
        matched = next(
            (s for s in _IMAGE_PULL_STATUSES if s.upper() in line_upper),
            None,
        )
        if matched:
            candidates.append((pod_name, matched))

    if not candidates:
        return []

    # Cap how many we describe to avoid hammering the API server
    to_describe = candidates[:max_describe]

    def _describe_one(name: str, matched_status: str) -> dict:
        desc = run_cli_command(["kubectl", "describe", "pod", name], namespace=namespace)
        desc_text = desc.stdout if desc.returncode == 0 else ""
        image = None
        event_lines: list[str] = []
        for dl in desc_text.splitlines():
            if not image:
                m = _IMAGE_RE.search(dl)
                if m:
                    image = m.group(1)
            if _FAILED_RE.search(dl):
                event_lines.append(dl.strip())
        return {
            "name":      name,
            "namespace": namespace,
            "status":    matched_status,
            "image":     image,
            "message":   "\n".join(event_lines[:3]) if event_lines else "(no detail available)",
        }

    # Parallel describe
    workers = min(10, len(to_describe))
    ordered: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_describe_one, name, status): name
            for name, status in to_describe
        }
        for future in as_completed(future_map):
            pod = future.result()
            ordered[pod["name"]] = pod

    # Return in original order
    failing = [ordered[name] for name, _ in to_describe if name in ordered]

    # If we capped, append stub entries for the remaining pods (same image, no describe)
    if len(candidates) > max_describe:
        extra_count = len(candidates) - max_describe
        # Best-effort: reuse the first image we found
        first_image = next((p["image"] for p in failing if p.get("image")), None)
        for name, matched_status in candidates[max_describe:]:
            failing.append({
                "name":      name,
                "namespace": namespace,
                "status":    matched_status,
                "image":     first_image,
                "message":   "(not described — too many pods)",
            })

    return failing


def check_local_registry(host: str = "host.docker.internal", port: int = 5050) -> dict:
    """TCP-probe the local registry.  Returns a result dict.

    ``host.docker.internal`` only resolves inside Docker containers — on the
    host machine it raises ENOTFOUND (errno 8).  When that happens we
    transparently retry against ``localhost`` on the same port, since the
    registry is typically published with ``-p 5050:5000``.
    """
    for attempt_host in (host, "localhost"):
        try:
            with socket.create_connection((attempt_host, port), timeout=3):
                return {
                    "reachable": True,
                    "host": attempt_host,
                    "port": port,
                    "error": None,
                    "note": f"resolved via {attempt_host!r}" if attempt_host != host else None,
                }
        except socket.gaierror:
            # DNS failure — try the next candidate
            continue
        except OSError as exc:
            # Port reachable host but connection refused / timed out
            return {"reachable": False, "host": attempt_host, "port": port, "error": str(exc)}
    # Both candidates failed to resolve
    return {
        "reachable": False,
        "host": host,
        "port": port,
        "error": (
            f"{host!r} does not resolve on the host (it only resolves inside Docker containers). "
            f"Also tried localhost:{port} — DNS lookup failed for both."
        ),
    }


def _detect_registry_mirror_issue(failing: list[dict]) -> bool:
    """Heuristic: message contains 'registry-mirror' or pull from localhost:5050."""
    for pod in failing:
        msg = (pod.get("message") or "").lower()
        img = (pod.get("image") or "").lower()
        if "registry-mirror" in msg or "5050" in img or "host.docker.internal" in img:
            return True
    return False


def _detect_mirror_500(failing: list[dict]) -> bool:
    """Return True if any pod event shows a 500 from the registry-mirror proxy.

    This happens when containerd routes the pull through registry-mirror:1273 but
    the image doesn't exist at host.docker.internal:5050 yet (e.g. push never
    completed).
    """
    for pod in failing:
        msg = pod.get("message") or ""
        if "registry-mirror" in msg and "500" in msg:
            return True
    return False


def _get_kind_cluster_name() -> str:
    """Return the first kind cluster name, or 'kind' as a default."""
    try:
        r = subprocess.run(
            ["kind", "get", "clusters"],
            capture_output=True, text=True, timeout=10,
        )
        clusters = [c.strip() for c in r.stdout.splitlines() if c.strip()]
        return clusters[0] if clusters else "kind"
    except Exception:
        return "kind"


def _kind_load_images(failing: list[dict]) -> list[dict]:
    """Run ``kind load docker-image <bare-image>`` for every unique image in
    *failing*.  Strips the registry prefix before loading — kind loads from the
    local Docker daemon by bare name.

    Returns a list of result dicts: ``{"image": str, "returncode": int, "stderr": str}``.
    """
    registry = "host.docker.internal:5050"
    images = list(dict.fromkeys(
        pod["image"].replace(f"{registry}/", "")
        for pod in failing if pod.get("image")
    ))
    cluster = _get_kind_cluster_name()
    results: list[dict] = []
    for img in images:
        print(f"  $ kind load docker-image {img} --name {cluster}")
        try:
            r = subprocess.run(
                ["kind", "load", "docker-image", img, "--name", cluster],
                capture_output=True, text=True, timeout=300,
            )
        except FileNotFoundError:
            results.append({"image": img, "returncode": 127, "stderr": "kind not found in PATH"})
            print("    [ERROR] kind not found in PATH")
            break
        except subprocess.TimeoutExpired:
            results.append({"image": img, "returncode": -1, "stderr": "timed out after 300s"})
            print("    [ERROR] timed out after 300s")
            break
        if r.returncode == 0:
            print("    [OK]")
        else:
            err = (r.stderr or r.stdout or "").strip()
            print(f"    [FAIL] {err[:200]}")
        results.append({"image": img, "returncode": r.returncode, "stderr": r.stderr or ""})
    return results


# ── rendering ─────────────────────────────────────────────────────────────────

def render_image_pull_diagnosis(failing: list[dict], registry_result: dict | None = None) -> None:
    """Print a formatted diagnosis box to stdout."""
    clear_screen()
    W = 60

    print("╔" + "═" * W + "╗")
    print("║" + "  IMAGE PULL DEBUGGER".center(W) + "║")
    print("╠" + "═" * W + "╣")

    if not failing:
        print("║" + "  ✓ No image pull errors detected".ljust(W) + "║")
        print("╚" + "═" * W + "╝")
        return

    count_label = f"  ✗ {len(failing)} pod(s) with image pull errors:"
    print("║" + count_label.ljust(W) + "║")
    print("╠" + "─" * W + "╣")

    for pod in failing:
        name_line = f"  Pod: {pod['name']}"
        print("║" + name_line.ljust(W) + "║")

        status_line = f"    Status : {pod['status']}"
        print("║" + status_line.ljust(W) + "║")

        if pod.get("image"):
            img = pod["image"]
            if len(img) > W - 14:
                img = "…" + img[-(W - 15):]
            print("║" + f"    Image  : {img}".ljust(W) + "║")

        if pod.get("message"):
            for raw in pod["message"].splitlines():
                for chunk in textwrap.wrap(raw, W - 6):
                    print("║" + f"    {chunk}".ljust(W) + "║")

        print("╠" + "─" * W + "╣")

    # Registry probe result (if already run)
    if registry_result is not None:
        if registry_result["reachable"]:
            reg_line = f"  ✓ Local registry {registry_result['host']}:{registry_result['port']} reachable"
        else:
            reg_line = f"  ✗ Local registry unreachable: {registry_result['error']}"
        print("║" + reg_line.ljust(W) + "║")
        print("╠" + "─" * W + "╣")

    # Diagnosis hint
    if _detect_mirror_500(failing):
        print("║" + (" ✗ Root cause: Docker Desktop mirror → HTTPS mismatch").ljust(W) + "║")
        print("║" + ("   Every kind node's containerd routes ALL pulls via").ljust(W) + "║")
        print("║" + ("   registry-mirror:1273 (Docker Desktop built-in).").ljust(W) + "║")
        print("║" + ("   That mirror tries HTTPS to host.docker.internal:5050").ljust(W) + "║")
        print("║" + ("   but the local registry speaks plain HTTP → 500.").ljust(W) + "║")
        print("║" + ("   Fix: write a certs.d/host.docker.internal:5050/").ljust(W) + "║")
        print("║" + ("   hosts.toml on each node so containerd bypasses the").ljust(W) + "║")
        print("║" + ("   mirror for this registry and uses HTTP directly.").ljust(W) + "║")
    else:
        hint = "  Likely cause: image not present in local registry, or"
        print("║" + hint.ljust(W) + "║")
        hint2 = "  registry mirror misconfiguration in containerd."
        print("║" + hint2.ljust(W) + "║")

    print("╚" + "═" * W + "╝")


# ── fix actions ───────────────────────────────────────────────────────────────

def _image_exists_locally(name: str) -> bool:
    """Return True if *name* is already present in the local Docker daemon."""
    try:
        result = subprocess.run(  # noqa: S603
            ["docker", "images", "-q", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return bool(result.stdout.strip())
    except Exception:  # docker unavailable, timeout, etc.
        return False


def _build_push_steps(failing: list[dict]) -> list[tuple[str, list[str]]]:
    """Return a list of (description, argv) tuples for the full push batch.

    Each image yields up to three steps: pull (skipped if already local), tag, push.
    Images already prefixed with the local registry have their prefix stripped
    before pulling so we don't try to pull from a possibly-broken mirror.
    Locally-built images (never pushed to a public registry) already exist in
    the Docker daemon, so the pull step is skipped for them entirely.
    """
    registry = "host.docker.internal:5050"
    images = list(dict.fromkeys(pod["image"] for pod in failing if pod.get("image")))
    steps: list[tuple[str, list[str]]] = []
    for img in images:
        bare = img.replace(f"{registry}/", "")
        tagged = f"{registry}/{bare}"
        if not _image_exists_locally(bare):
            steps.append((f"docker pull {bare}",    ["docker", "pull", bare]))
        steps.append((f"docker tag  {bare} {tagged}", ["docker", "tag",  bare, tagged]))
        steps.append((f"docker push {tagged}",  ["docker", "push", tagged]))
    return steps


def _show_push_commands(failing: list[dict]) -> None:
    """Print the docker push commands the user needs to run."""
    print("\n  To populate the local registry, run the following commands:\n")
    steps = _build_push_steps(failing)
    if not steps:
        print("  (Could not determine image names — check 'kubectl describe pod <name>')")
        return
    prev_img = None
    for desc, _ in steps:
        # Blank line between image groups (every 3 steps)
        cmd_verb = desc.split()[1] if len(desc.split()) > 1 else ""
        if cmd_verb == "pull":
            if prev_img is not None:
                print()
            prev_img = desc
        print(f"    {desc}")
    print()


# ── Docker insecure-registry auto-fix ────────────────────────────────────────

import json as _json
import os as _os
import sys as _sys
import time as _time

_DAEMON_JSON = _os.path.expanduser("~/.docker/daemon.json")


def _is_insecure_registry_configured(registry: str) -> bool:
    """Return True if *registry* is already in ~/.docker/daemon.json."""
    try:
        with open(_DAEMON_JSON) as f:
            data = _json.load(f)
        return registry in data.get("insecure-registries", [])
    except (FileNotFoundError, _json.JSONDecodeError):
        return False


def configure_insecure_registry(registry: str) -> bool:
    """Add *registry* to ~/.docker/daemon.json insecure-registries.

    Returns True if the file was modified, False if already configured.
    Raises on permission or JSON errors.
    """
    try:
        with open(_DAEMON_JSON) as f:
            data = _json.load(f)
    except FileNotFoundError:
        data = {}
    except _json.JSONDecodeError as exc:
        raise ValueError(f"~/.docker/daemon.json is not valid JSON: {exc}") from exc

    existing = data.get("insecure-registries", [])
    if registry in existing:
        return False  # already there

    data["insecure-registries"] = existing + [registry]
    with open(_DAEMON_JSON, "w") as f:
        _json.dump(data, f, indent=2)
        f.write("\n")
    return True


def _restart_docker_desktop() -> bool:
    """Quit and relaunch Docker Desktop (macOS only). Returns True on success."""
    if _sys.platform != "darwin":
        return False
    # Quit Docker Desktop
    subprocess.run(
        ["osascript", "-e", 'quit app "Docker Desktop"'],
        capture_output=True, timeout=15,
    )
    _time.sleep(3)
    # Relaunch
    result = subprocess.run(
        ["open", "-a", "Docker Desktop"],
        capture_output=True, timeout=15,
    )
    return result.returncode == 0


def _wait_for_docker(timeout: int = 60) -> bool:
    """Poll until `docker info` succeeds or *timeout* seconds elapse."""
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            return True
        _time.sleep(3)
    return False


def _results_have_https_error(results: list[dict]) -> bool:
    return any(
        "http: server gave HTTP response to HTTPS client" in r.get("stderr", "")
        for r in results
    )


def _fix_insecure_registry_interactive(registry: str) -> bool:
    """Walk the user through patching daemon.json and restarting Docker Desktop.

    Returns True when Docker is ready and the fix was applied.
    """
    bold = "\033[1m"
    green = "\033[32m"
    yellow = "\033[33m"
    reset = "\033[0m"

    print(f"\n{bold}Fixing insecure registry: {registry}{reset}")

    # Step 1: patch daemon.json
    try:
        changed = configure_insecure_registry(registry)
    except (ValueError, OSError) as exc:
        print(f"  {yellow}[WARN]{reset} Could not update {_DAEMON_JSON}: {exc}")
        print(f"  Edit it manually and add: \"insecure-registries\": [\"{registry}\"]")
        return False

    if changed:
        print(f"  {green}✓{reset} Added {registry!r} to {_DAEMON_JSON}")
    else:
        print(f"  {green}✓{reset} {registry!r} is already in {_DAEMON_JSON}")

    # Step 2: restart Docker Desktop (macOS only)
    if _sys.platform == "darwin":
        print(f"  Restarting Docker Desktop…")
        launched = _restart_docker_desktop()
        if not launched:
            print(f"  {yellow}[WARN]{reset} Could not relaunch Docker Desktop automatically.")
            print("  Please restart it manually, then retry.")
            return False
        print("  Waiting for Docker daemon to be ready…", end="", flush=True)
        ready = _wait_for_docker(timeout=90)
        if ready:
            print(f" {green}ready{reset}")
        else:
            print(f" {yellow}timed out{reset}")
            print("  Docker may still be starting. Wait a moment and retry.")
            return False
    else:
        print(f"  {yellow}[INFO]{reset} Restart Docker manually to apply the change, then retry.")
        return False

    return True


def run_push_commands(failing: list[dict]) -> list[dict]:
    """Execute the docker pull/tag/push batch.  Returns a results list.

    Each entry::

        {"cmd": str, "returncode": int, "stderr": str}
    """
    steps = _build_push_steps(failing)
    results: list[dict] = []
    for desc, argv in steps:
        print(f"  $ {desc}")
        try:
            proc = subprocess.run(  # noqa: S603  — argv list, no shell
                argv,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            results.append({"cmd": desc, "returncode": 127, "stderr": "docker not found in PATH"})
            print("    [ERROR] docker not found in PATH")
            break
        except subprocess.TimeoutExpired:
            results.append({"cmd": desc, "returncode": -1, "stderr": "timed out after 120s"})
            print("    [ERROR] timed out after 120s")
            break
        if proc.returncode == 0:
            print("    [OK]")
        else:
            err = (proc.stderr or "").strip()
            print(f"    [FAIL] {err[:200]}")
            if "http: server gave HTTP response to HTTPS client" in err:
                print()
                print("    [HINT] Docker is trying HTTPS but the local registry uses plain HTTP.")
                print("    Add it to Docker's insecure registries:")
                print("      Docker Desktop → Settings → Docker Engine → add:")
                print('        "insecure-registries": ["host.docker.internal:5050"]')
                print("    Then click Apply & Restart, and retry.")
                print()
        results.append({"cmd": desc, "returncode": proc.returncode, "stderr": proc.stderr or ""})
        if proc.returncode != 0:
            print("  Stopping batch — fix the error above and retry.")
            break
    return results


def _get_kind_node_containers() -> list[str]:
    """Return docker container names for all kind nodes (workers + control-plane)."""
    try:
        r = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        return [
            n.strip() for n in r.stdout.splitlines()
            if n.strip() and ("worker" in n or "control-plane" in n)
        ]
    except Exception:
        return []


_REGISTRY_HOSTS_TOML = (
    'server = "http://host.docker.internal:5050"\n\n'
    '[host."http://host.docker.internal:5050"]\n'
    '  capabilities = ["pull", "resolve", "push"]\n'
    '  skip_verify = true\n'
)


def _patch_node_registry_http(node: str) -> tuple[bool, str]:
    """Write certs.d/host.docker.internal:5050/hosts.toml on a kind node.

    This tells containerd to bypass the Docker Desktop mirror and connect
    directly via HTTP for host.docker.internal:5050.
    Returns (ok, error_message).
    """
    cmd = (
        'mkdir -p /etc/containerd/certs.d/host.docker.internal:5050 && '
        f'printf {subprocess.list2cmdline([_REGISTRY_HOSTS_TOML])} '
        '> /etc/containerd/certs.d/host.docker.internal:5050/hosts.toml'
    )
    try:
        r = subprocess.run(
            ["docker", "exec", node, "sh", "-c", cmd],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or r.stdout or "").strip()
    except Exception as exc:
        return False, str(exc)


def _run_patch_all_nodes() -> tuple[int, int]:
    """Apply the HTTP registry fix to every kind node in parallel.

    Returns (ok_count, total_count).
    """
    nodes = _get_kind_node_containers()
    if not nodes:
        return 0, 0
    ok = 0
    with ThreadPoolExecutor(max_workers=min(len(nodes), 8)) as pool:
        futures = {pool.submit(_patch_node_registry_http, n): n for n in nodes}
        for future in as_completed(futures):
            node = futures[future]
            success, err = future.result()
            if success:
                ok += 1
                print(f"  ✓ {node}")
            else:
                print(f"  ✗ {node}: {err[:80]}")
    return ok, len(nodes)


def _show_containerd_patch() -> None:
    """Print the certs.d hosts.toml fix that bypasses the Docker Desktop mirror."""
    snippet = textwrap.dedent("""\
        # Written to every kind node at:
        # /etc/containerd/certs.d/host.docker.internal:5050/hosts.toml
        #
        # Tells containerd: for this registry, skip the Docker Desktop
        # registry-mirror and connect directly via plain HTTP.
        # No containerd restart required — takes effect on the next pull.

        server = "http://host.docker.internal:5050"

        [host."http://host.docker.internal:5050"]
          capabilities = ["pull", "resolve", "push"]
          skip_verify = true
    """)
    print("\n" + "─" * 64)
    print("  containerd per-registry HTTP override (hosts.toml):")
    print("─" * 64)
    for line in snippet.splitlines():
        print("  " + line)
    print("─" * 64)
    print("  NOTE: This is ephemeral — re-run after cluster is recreated.")
    print("─" * 64)
    print()


def _show_pull_policy_patch(failing: list[dict]) -> None:
    """Show kubectl patch commands to set imagePullPolicy: IfNotPresent."""
    print("\n  To set imagePullPolicy: IfNotPresent on affected pod owners:\n")
    printed_owners: set[str] = set()
    for pod in failing:
        name = pod["name"]
        # Describe pod to find owner reference
        desc = run_cli_command(["kubectl", "describe", "pod", name], namespace=pod["namespace"])
        owner_kind = None
        owner_name = None
        if desc.returncode == 0:
            for line in desc.stdout.splitlines():
                if line.strip().startswith("Controlled By:"):
                    parts = line.split(":", 1)[1].strip().split("/")
                    if len(parts) == 2:
                        owner_kind, owner_name = parts[0].strip().lower(), parts[1].strip()
                    break

        if owner_kind and owner_name and owner_name not in printed_owners:
            printed_owners.add(owner_name)
            print(
                f"    kubectl patch {owner_kind} {owner_name} "
                f"--namespace {pod['namespace']} "
                "--type=json "
                "-p='[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/imagePullPolicy\",\"value\":\"IfNotPresent\"}]'"
            )
        elif not owner_kind:
            print(f"    # {name}: could not determine owner — patch manually")
    print()


def delete_failing_pods(failing: list[dict]) -> list[dict]:
    """Delete each pod in *failing* so it is rescheduled by its owner.

    Returns a results list, one entry per pod::

        {"name": str, "returncode": int, "stderr": str}
    """
    results: list[dict] = []
    for pod in failing:
        r = run_cli_command(
            ["kubectl", "delete", "pod", pod["name"]],
            namespace=pod["namespace"],
        )
        results.append({
            "name":       pod["name"],
            "returncode": r.returncode,
            "stderr":     r.stderr or "",
        })
        marker = "[OK]" if r.returncode == 0 else "[FAIL]"
        print(f"  {marker} deleted {pod['name']}")
    return results


# ── main menu ─────────────────────────────────────────────────────────────────

def image_pull_menu(namespace: str = "default") -> None:
    """Top-level image pull debugger menu."""
    failing: list[dict] = []
    registry_result: dict | None = None
    first_run = True

    while True:
        if first_run:
            print("\n[INFO] Scanning for image pull errors…")
            failing = scan_image_pull_errors(namespace)
            first_run = False

        render_image_pull_diagnosis(failing, registry_result)

        choices: list = []

        if failing:
            if _detect_mirror_500(failing):
                choices.append(questionary.Choice(
                    title="Fix containerd mirror  (write hosts.toml on all nodes — no restart needed)",
                    value="patch_nodes",
                ))
                choices.append(questionary.Choice(
                    title="Load image into kind nodes  (kind load docker-image — bypasses mirror)",
                    value="kind_load",
                ))
            choices.append(questionary.Choice(
                title="Check local registry reachability",
                value="check_registry",
            ))
            choices.append(questionary.Choice(
                title="Show: docker push commands to populate registry",
                value="show_push",
            ))
            choices.append(questionary.Choice(
                title="Run: docker pull/tag/push batch now",
                value="run_push",
            ))
            choices.append(questionary.Choice(
                title="Show: containerd hosts.toml override (for manual apply)",
                value="show_patch",
            ))
            choices.append(questionary.Choice(
                title="Show: imagePullPolicy patch commands",
                value="show_pull_policy",
            ))
            choices.append(questionary.Choice(
                title=f"Delete {len(failing)} failing pod(s) — force restart",
                value="delete_pods",
            ))

        choices.extend([
            questionary.Choice(title="Re-scan pods", value="rescan"),
            questionary.Separator(),
            questionary.Choice(title="Back", value="back"),
        ])

        try:
            action = questionary.select(
                "What would you like to do?",
                choices=choices,
                style=custom_style,
            ).ask()
        except KeyboardInterrupt:
            return

        if not action or action == "back":
            return

        if action == "rescan":
            print("\n[INFO] Rescanning…")
            failing = scan_image_pull_errors(namespace)
            registry_result = None
            continue

        if action == "patch_nodes":
            print()
            print("  Writing certs.d/host.docker.internal:5050/hosts.toml on all kind nodes…")
            print("  (This tells containerd to use HTTP directly, bypassing the Docker Desktop mirror)")
            print()
            ok, total = _run_patch_all_nodes()
            if total == 0:
                print("  [WARN] No kind nodes found via docker ps.")
            elif ok == total:
                print(f"\n  [OK] Patched {ok}/{total} node(s).")
                print("  containerd reads certs.d at pull time — no restart needed.")
                print("  Deleting failing pods so they re-pull…")
                print()
                delete_failing_pods(failing)
                print("\n  Re-scanning…")
                failing = scan_image_pull_errors(namespace)
                registry_result = None
            else:
                print(f"\n  [WARN] Only {ok}/{total} node(s) patched — check errors above.")
            _prompt_continue()
            continue

        if action == "kind_load":
            images = list(dict.fromkeys(
                pod["image"].replace("host.docker.internal:5050/", "")
                for pod in failing if pod.get("image")
            ))
            missing = [img for img in images if not _image_exists_locally(img)]
            if missing:
                print()
                print(f"  [WARN] The following image(s) are not in the local Docker daemon:")
                for img in missing:
                    print(f"    · {img}")
                print("  Build the image first, e.g.:")
                print("    docker build -t playwright-agent .")
                _prompt_continue()
                continue
            print()
            results = _kind_load_images(failing)
            ok = sum(1 for r in results if r["returncode"] == 0)
            total = len(results)
            print(f"\n  Loaded {ok}/{total} image(s) into kind cluster.")
            if ok == total:
                print("  [OK] All images loaded. Deleting failing pods so they restart…")
                print()
                delete_failing_pods(failing)
                print("\n  Re-scanning…")
                failing = scan_image_pull_errors(namespace)
                registry_result = None
            _prompt_continue()
            continue

        if action == "check_registry":
            print("\n[INFO] Probing local registry…")
            registry_result = check_local_registry()
            if registry_result["reachable"]:
                note = registry_result.get("note")
                resolved = registry_result['host']
                msg = f"  [OK] {resolved}:{registry_result['port']} is reachable."
                if note:
                    msg += f"  (Note: host.docker.internal did not resolve; used localhost instead)"
                print(msg)
            else:
                err = registry_result['error']
                print(
                    f"  [WARN] Cannot reach registry on port {registry_result['port']}:"
                    f" {err}"
                )
                if "does not resolve" in (err or ""):
                    print(
                        "  Note: 'host.docker.internal' is a Docker-internal hostname — "
                        "it only resolves inside containers, not on the host.\n"
                        "  The CLI automatically retried 'localhost' but that also failed.\n"
                        "  Ensure the registry container is running and published on port 5050:\n"
                        "    docker run -d -p 5050:5000 registry:2"
                    )
                else:
                    print(
                        "  Tip: ensure your local registry container is running\n"
                        "  (e.g. docker run -d -p 5050:5000 registry:2)"
                    )
            _prompt_continue()
            continue

        if action == "show_push":
            _show_push_commands(failing)
            _prompt_continue()
            continue

        if action == "run_push":
            steps = _build_push_steps(failing)
            if not steps:
                print("  [WARN] No images to push — could not determine image names.")
                _prompt_continue()
                continue
            print()
            _show_push_commands(failing)
            confirmed = questionary.confirm(
                f"Run {len(steps)} docker command(s) now?",
                default=False,
                style=custom_style,
            ).ask()
            if confirmed:
                print()
                results = run_push_commands(failing)
                ok = sum(1 for r in results if r["returncode"] == 0)
                total = len(results)
                print(f"\n  Completed {ok}/{total} step(s).")
                if ok == total:
                    print("  [OK] All images pushed.")
                    wants_delete = questionary.confirm(
                        f"Delete {len(failing)} failing pod(s) now so they restart and pull the image?",
                        default=True,
                        style=custom_style,
                    ).ask()
                    if wants_delete:
                        print()
                        delete_failing_pods(failing)
                    print("\n  Re-scanning…")
                    failing = scan_image_pull_errors(namespace)
                    registry_result = None
                    _prompt_continue()
                else:
                    # Some steps failed — detect whether it was an HTTPS error
                    # so we can offer the automated insecure-registry fix.
                    https_error = _results_have_https_error(results)
                    next_choices = []
                    if https_error and not _is_insecure_registry_configured("host.docker.internal:5050"):
                        next_choices.append(questionary.Choice(
                            title="Fix insecure registry & retry  (patch daemon.json + restart Docker Desktop)",
                            value="fix_and_retry",
                        ))
                    elif https_error:
                        # Already in daemon.json — Docker may not have restarted yet
                        next_choices.append(questionary.Choice(
                            title="Restart Docker Desktop & retry",
                            value="restart_and_retry",
                        ))
                    next_choices.append(questionary.Choice(title="Retry batch", value="retry"))
                    next_choices.append(questionary.Choice(title="Continue", value="continue"))

                    next_action = questionary.select(
                        "Next:",
                        choices=next_choices,
                        style=custom_style,
                    ).ask()

                    if next_action in ("fix_and_retry", "restart_and_retry"):
                        registry = "host.docker.internal:5050"
                        if next_action == "restart_and_retry":
                            # Already in daemon.json, just restart
                            import sys as _sys2
                            if _sys2.platform == "darwin":
                                print("\n  Restarting Docker Desktop…")
                                _restart_docker_desktop()
                                print("  Waiting for Docker daemon…", end="", flush=True)
                                ready = _wait_for_docker(timeout=90)
                                print(" ready" if ready else " timed out")
                                if not ready:
                                    _prompt_continue()
                                    # fall through to continue
                                    next_action = "continue"
                            else:
                                next_action = "retry"  # just retry on non-macOS
                        else:
                            fixed = _fix_insecure_registry_interactive(registry)
                            if not fixed:
                                _prompt_continue()
                                next_action = "continue"

                    if next_action in ("fix_and_retry", "restart_and_retry", "retry"):
                        print()
                        results = run_push_commands(failing)
                        ok = sum(1 for r in results if r["returncode"] == 0)
                        total = len(results)
                        print(f"\n  Completed {ok}/{total} step(s).")
                        if ok == total:
                            print("  [OK] All images pushed.")
                            wants_delete = questionary.confirm(
                                f"Delete {len(failing)} failing pod(s) now so they restart and pull the image?",
                                default=True,
                                style=custom_style,
                            ).ask()
                            if wants_delete:
                                print()
                                delete_failing_pods(failing)
                            print("\n  Re-scanning…")
                            failing = scan_image_pull_errors(namespace)
                            registry_result = None
                        _prompt_continue()
                    # else: "continue" — fall through normally
            else:
                _prompt_continue()
            continue

        if action == "show_patch":
            _show_containerd_patch()
            _prompt_continue()
            continue

        if action == "show_pull_policy":
            _show_pull_policy_patch(failing)
            _prompt_continue()
            continue

        if action == "delete_pods":
            print(f"\n  This will delete {len(failing)} pod(s). Their owner (Job/Deployment) will")
            print("  recreate them immediately and retry the image pull.")
            confirmed = questionary.confirm(
                f"Delete {len(failing)} failing pod(s) now?",
                default=False,
                style=custom_style,
            ).ask()
            if confirmed:
                print()
                delete_failing_pods(failing)
                print("\n  Re-scanning…")
                failing = scan_image_pull_errors(namespace)
                registry_result = None
            _prompt_continue()
            continue
