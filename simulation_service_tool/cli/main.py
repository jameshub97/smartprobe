"""Main interactive menu loop.

CLI boot sequence
-----------------
Entry point: ``interactive_menu()``

  1. ``_early_k8s_check()``
     **First thing on launch** — before any other output or checks.
     Calls ``k8s_reachable()`` with a short timeout and prints a single
     status line:
       ● Kubernetes API ready              → continue silently
       ⚠ Kubernetes API is unreachable    → recovery loop with targeted fixes
     This is a hard prerequisite gate — Docker + K8s must be working before
     the CLI proceeds.  The recovery loop (``_k8s_recovery_loop()``) offers:
       "Restart Kind container (<name>)"   →  docker restart <name>
       "Delete & recreate Kind cluster"    →  kind delete + create
       "Start Kind container (<name>)"     →  docker start <name>
       "Create new Kind cluster"           →  kind create cluster
       "Start minikube cluster"            →  minikube start
       "Show diagnostic details"           →  collect_failure_details()
       "Re-check Kubernetes API"           →  retry probe
       "Exit"                              →  stop before welcome_menu

  2. ``_early_api_check()``
     Hits ``http://localhost:5002/health`` with a 2 s timeout:
       ● Simulation service ready          → continue silently
       ⚠ Simulation service is offline    → prompt: start now / continue anyway

  3. ``_run_startup_diagnostics()``
     Full parallel probe table — all seven checks run concurrently:
       Simulation service  HTTP GET http://localhost:5002/health
       Backend API         HTTP GET http://localhost:5001/health
       Transfer Stacker    HTTP GET http://localhost:5173/
       PostgreSQL          TCP connect localhost:5432
       Docker API          smart_diagnostics._docker_running()
       Cluster runtime     k8s_connectivity.cluster_runtime_status()
       Kubernetes API      k8s_connectivity.k8s_reachable()
     Hard exit: Docker API unreachable → print error, return False.

  4. ``_run_docker_health_check()``
     Prints each Docker Compose service with a coloured health status and
     prompts the user to continue or abort if any service is unhealthy.
     If the stack is not running the user is offered Continue / Abort.
     If all services are healthy it auto-continues without prompting.

  5. ``welcome_menu()``  (menus/welcome.py)
     Determines which menu variant to show based on three state flags:
       • ``service_running``      — simulation API responds on :5002
       • ``cluster_initialized``  — cluster_init.is_initialized() returns True
       • ``drift_findings``       — smart_diagnostics.run_drift_checks() results

Smart-diagnostics integration points
-------------------------------------
To add a new boot-time check:
  • Add a ``(label, callable)`` tuple to ``_CHECKS``.
  • Add a status style entry to ``_STATUS_STYLE`` if the return value is new.

Log sink: all urllib3/kubernetes SDK output is redirected to
``simulation_service_tool.log`` (project root) so nothing corrupts the TUI.
"""

import logging
import os
import socket

from simulation_service_tool.menus.welcome import welcome_menu

# Redirect all log output from background libraries to a file so nothing
# writes to stdout/stderr while the spinner or questionary prompt is active.
# 504/gateway-timeout errors from k8s are expected when the cluster is
# unreachable and must not corrupt the terminal.
_LOG_FILE = os.path.join(os.path.dirname(__file__), '..', '..', 'simulation_service_tool.log')
_file_handler = logging.FileHandler(os.path.normpath(_LOG_FILE))
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

_REDIRECTED_LOGGERS = [
    'simulation_service',
    'kubernetes',
    'kubernetes.client',
    'kubernetes.client.rest',
    'urllib3',
    'urllib3.connectionpool',
]
for _logger_name in _REDIRECTED_LOGGERS:
    _lg = logging.getLogger(_logger_name)
    _lg.handlers = [_file_handler]
    _lg.propagate = False   # don't bubble up to root handler (stderr)


# ---------------------------------------------------------------------------
# Startup diagnostics
# ---------------------------------------------------------------------------

def _tcp_reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_status(url: str, timeout: float = 2.0) -> str:
    """Return HTTP status code string, or an error label."""
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return str(resp.status)
    except urllib.error.HTTPError as exc:
        return str(exc.code)
    except Exception:
        return "unreachable"


def _k8s_reachable() -> str:
    """Quick non-blocking check for the Kubernetes API.

    Delegates to the k8s_connectivity service module.
    """
    from simulation_service_tool.services.k8s_connectivity import k8s_reachable
    return k8s_reachable()


def _docker_api_status() -> str:
    from simulation_service_tool.services.smart_diagnostics import _docker_running
    return "reachable" if _docker_running() else "unreachable"


def _cluster_runtime_status() -> str:
    from simulation_service_tool.services.k8s_connectivity import cluster_runtime_status
    return cluster_runtime_status()


_CHECKS = [
    ("Simulation service",  lambda: _http_status("http://localhost:5002/health")),
    ("Coordinator",         lambda: _http_status("http://localhost:5003/api/coordinator/health")),
    ("Backend API",         lambda: _http_status("http://localhost:5001/health")),
    ("Transfer Stacker",    lambda: _http_status("http://localhost:5173/")),
    ("PostgreSQL",          lambda: "reachable" if _tcp_reachable("localhost", 5432) else "unreachable"),
    ("Local registry",      lambda: "reachable" if _tcp_reachable("localhost", 5050) else "unreachable"),
    ("Docker API",          _docker_api_status),
    ("Cluster runtime",     _cluster_runtime_status),
    ("Kubernetes API",      _k8s_reachable),
]

_STATUS_STYLE = {
    "200": "\033[32m● 200 ok\033[0m",
    "reachable": "\033[32m● reachable\033[0m",
    "unreachable": "\033[33m○ unreachable\033[0m",
    "timeout": "\033[33m○ timeout\033[0m",
    "kubectl not found": "\033[33m○ kubectl not found\033[0m",
    "kind running": "\033[32m● kind running\033[0m",
    "kind configured": "\033[33m○ kind configured\033[0m",
    "kind stopped": "\033[33m○ kind stopped\033[0m",
    "kind available": "\033[33m○ kind available\033[0m",
    "minikube running": "\033[32m● minikube running\033[0m",
    "minikube stopped": "\033[33m○ minikube stopped\033[0m",
    "docker-desktop selected": "\033[33m○ docker-desktop selected\033[0m",
    "no runtime": "\033[31m✗ no runtime\033[0m",
}


def _render_status(raw: str) -> str:
    if raw in _STATUS_STYLE:
        return _STATUS_STYLE[raw]
    if raw.isdigit() and int(raw) < 400:
        return f"\033[32m● {raw} ok\033[0m"
    if raw.startswith("error:") or (raw.isdigit() and int(raw) >= 400):
        return f"\033[31m✗ {raw}\033[0m"
    return f"\033[33m○ {raw}\033[0m"


def _print_docker_failure():
    red   = "\033[31m"
    bold  = "\033[1m"
    dim   = "\033[2m"
    reset = "\033[0m"
    print(f"\n  {red}{bold}Failed to connect to Docker API — exiting.{reset}")
    print(f"  {dim}Check Docker Desktop is running: open Docker Desktop and wait")
    print(f"  for the whale icon to stop animating.{reset}")
    print(f"  {dim}Then: Settings → Kubernetes → Enable Kubernetes → Apply & Restart.{reset}\n")



# ---------------------------------------------------------------------------
# Extended k8s failure diagnostics
# ---------------------------------------------------------------------------

def _print_k8s_failure_details():
    from simulation_service_tool.services.k8s_connectivity import (
        collect_failure_details, format_failure_details,
    )
    details = collect_failure_details()
    if details:
        print(format_failure_details(details))
        print()


# ---------------------------------------------------------------------------
# Boot-time k8s prerequisite gate
# ---------------------------------------------------------------------------

# Context/minikube helpers now live in k8s_connectivity service module.
# Thin wrappers kept for internal use during boot only.


def _early_api_check() -> bool:
    """First thing on launch: check if the simulation service is reachable.

    Runs before the full diagnostics table so the user sees a clear prompt
    immediately — not buried after all six probe results.

    Returns True when it is safe to continue (service is up, or the user
    explicitly chose to proceed without it).  Returns False only on
    Ctrl-C / terminal interrupt.
    """
    import questionary
    from simulation_service_tool.ui.styles import custom_style

    green  = "\033[32m"
    yellow = "\033[33m"
    bold   = "\033[1m"
    dim    = "\033[2m"
    reset  = "\033[0m"

    print(f"\n  {dim}Checking simulation service API...{reset}", end="", flush=True)
    up = _http_status("http://localhost:5002/health")
    sim_ok = up.isdigit() and int(up) < 400

    if sim_ok:
        print(f"\r  {green}● Simulation service ready{reset}              ")
        return True

    # Service is offline — clear the checking line and prompt the user
    print(f"\r  {yellow}{bold}⚠ Simulation service is offline.{reset}       ")
    print(f"  {dim}Most test operations require it to be running.{reset}\n")

    try:
        action = questionary.select(
            "  How would you like to proceed?",
            choices=[
                questionary.Choice(title="Start the simulation service now (recommended)", value="start"),
                questionary.Choice(title="Continue without it", value="skip"),
            ],
            style=custom_style,
        ).ask()
    except (KeyboardInterrupt, Exception):
        print()
        return False

    if action == "start":
        from simulation_service_tool.services.smart_diagnostics import _restart_service
        print(f"\n  Starting simulation service...")
        success, detail = _restart_service()
        status = f"\033[32m[OK]\033[0m" if success else f"\033[33m[WARN]\033[0m"
        print(f"  {status} {detail}\n")
    elif not action:
        return False

    return True


def _early_k8s_check() -> bool:
    """First thing on launch: check if the Kubernetes API is reachable.

    Runs before the simulation service check and the diagnostics table so
    the user is not distracted by other probes when K8s is down.  This is a
    hard prerequisite gate — Docker + K8s must be working before the CLI
    proceeds.

    Returns True when it is safe to continue.
    Returns False only on Ctrl-C / terminal interrupt.
    """
    from simulation_service_tool.services.k8s_connectivity import k8s_reachable

    dim   = "\033[2m"
    green = "\033[32m"
    reset = "\033[0m"

    print(f"\n  {dim}Checking Kubernetes API...{reset}", end="", flush=True)
    if k8s_reachable() == "reachable":
        print(f"\r  {green}● Kubernetes API ready{reset}                  ")
        return True

    # Clear the checking line before entering the recovery loop
    print("\r" + " " * 50 + "\r", end="")
    return _k8s_recovery_loop()


def _k8s_recovery_loop() -> bool:
    """Interactive recovery loop for an unreachable Kubernetes API.

    Offers targeted fixes (restart Kind, recreate cluster, start minikube)
    and blocks until K8s is healthy or the user exits.
    """
    import questionary
    from simulation_service_tool.services.k8s_connectivity import (
        apply_recommendation,
        build_recommendations,
        collect_failure_details,
        format_failure_details,
        k8s_reachable,
        k8s_stability_check,
        is_kind_installed,
    )
    from simulation_service_tool.ui.styles import custom_style

    yellow = "\033[33m"
    green  = "\033[32m"
    bold   = "\033[1m"
    dim    = "\033[2m"
    reset  = "\033[0m"

    recs = None  # lazy-loaded; refreshed after fix attempts

    while True:
        if recs is None:
            print(f"  {dim}Scanning cluster state...{reset}", end="", flush=True)
            recs = build_recommendations()
            print(f"\r" + " " * 40 + "\r", end="")

        print(f"  {yellow}{bold}⚠ Kubernetes API is unreachable.{reset}")
        print(f"  {dim}Docker and Kubernetes are required before continuing.{reset}\n")

        choices = []
        for rec in recs:
            choices.append(questionary.Choice(
                title=f"{rec['label']}  ->  {rec['detail']}",
                value=("fix", rec['action']),
            ))

        choices.extend([
            questionary.Choice(title="Show diagnostic details", value=("details", None)),
            questionary.Choice(title="Re-check Kubernetes API", value=("retry", None)),
            questionary.Choice(title="Exit", value=("exit", None)),
        ])

        try:
            result = questionary.select(
                "  How would you like to proceed?",
                choices=choices,
                style=custom_style,
            ).ask()
        except (KeyboardInterrupt, Exception):
            print()
            return False

        if not result:
            return False

        kind, payload = result

        if kind == "exit":
            return False

        if kind == "retry":
            if k8s_reachable() == "reachable":
                return True
            print()
            continue

        if kind == "details":
            details = collect_failure_details()
            if details:
                print()
                print(format_failure_details(details))
            else:
                print(f"\n  {dim}No diagnostic details available.{reset}")
            print()
            continue

        print()
        success = apply_recommendation(payload, print_fn=print)
        if success and k8s_reachable() == "reachable":
            # Run a stability check — Docker Desktop K8s is known to appear
            # healthy for a few seconds then crash with EOF.
            print(f"\n  {bold}Verifying cluster stability...{reset}")
            check = k8s_stability_check(probes=3, interval=2.0, print_fn=print)
            if check['stable']:
                print(f"\n  {green}{bold}✓ Kubernetes API is stable and reachable.{reset}\n")
                return True
            if check['flaky']:
                print(f"\n  {yellow}{bold}⚠ Kubernetes API is unstable{reset}"
                      f" (passed {sum(1 for r in check['results'] if r == 'reachable')}"
                      f"/{len(check['results'])} probes).")
                print(f"  {dim}Docker Desktop Kubernetes is flaky — it appears healthy")
                print(f"  then crashes seconds later.{reset}")
                if is_kind_installed():
                    print(f"  {bold}Recommendation:{reset} Switch to Kind for reliable local development.")
                    print(f"  {dim}  kind create cluster && kubectl config use-context kind-kind{reset}")
                print()
                recs = None  # force re-scan with Kind promoted
                continue

        if success:
            print(f"\n  {yellow}Fix applied but Kubernetes API is still unreachable.{reset}")
        else:
            print(f"\n  {yellow}Could not apply fix automatically for the current context.{reset}")
        print()
        recs = None  # force re-scan on next iteration


def _run_startup_diagnostics() -> tuple:
    """Run all startup probes and print results.

    Returns (ok: bool, results: dict).
    Returns ok=False only if the Docker API is unavailable (nothing can work
    without it); otherwise always returns True.  K8s reachability is enforced
    by ``_early_k8s_check()`` after the table is shown.
    The sim service check is included in the table for completeness but the
    user has already been prompted by _early_api_check() before this runs.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print(f"\033[1mStartup diagnostics\033[0m")
    print("─" * 44)

    results = {}
    with ThreadPoolExecutor(max_workers=len(_CHECKS)) as pool:
        futures = {pool.submit(fn): label for label, fn in _CHECKS}
        for future in as_completed(futures):
            label = futures[future]
            try:
                results[label] = future.result()
            except Exception:
                results[label] = "error"

    for label, _ in _CHECKS:
        raw = results.get(label, "error")
        styled = _render_status(raw)
        print(f"  {label:<24} {styled}")

    print("─" * 44)

    if results.get("Docker API") != "reachable":
        _print_docker_failure()
        return False, results

    print()
    return True, results


def _open_dashboard():
    """Prompt user to open the simulation dashboard in the browser."""
    import questionary
    from simulation_service_tool.ui.styles import custom_style
    choice = questionary.select(
        "Dashboard is ready at http://localhost:5002",
        choices=[
            questionary.Choice("Open in browser", value="open"),
            questionary.Choice("Continue to CLI", value="skip"),
        ],
        style=custom_style,
    ).ask()
    if choice == "open":
        import webbrowser
        webbrowser.open("http://localhost:5002")


def _run_docker_health_check() -> bool:
    """Show Docker Compose service health at launch and ask user to confirm.

    Runs after the startup diagnostics table.  If the stack is not running or
    Docker is unavailable it prints a brief warning and gives the user the
    option to continue anyway or abort.

    Returns True when it is safe to proceed, False on Ctrl-C / abort.
    """
    import questionary
    from simulation_service_tool.ui.styles import custom_style
    from simulation_service_tool.services.docker_compose import (
        is_docker_available, is_compose_running, get_service_health, EXPECTED_SERVICES,
    )

    green  = "\033[32m"
    yellow = "\033[33m"
    red    = "\033[31m"
    bold   = "\033[1m"
    dim    = "\033[2m"
    reset  = "\033[0m"

    print(f"{bold}Docker service health{reset}")
    print("─" * 44)

    if not is_docker_available():
        print(f"  {'Docker Compose':<24} {yellow}○ Docker unavailable{reset}")
        print("─" * 44)
        print()
        return True  # non-fatal — user may only care about K8s

    if not is_compose_running():
        print(f"  {'Stack':<24} {yellow}○ not running{reset}")
        print("─" * 44)
        print()
        try:
            action = questionary.select(
                "  Docker Compose stack is not running. Continue anyway?",
                choices=[
                    questionary.Choice(title="Continue to CLI", value="continue"),
                    questionary.Choice(title="Abort", value="abort"),
                ],
                style=custom_style,
            ).ask()
        except (KeyboardInterrupt, Exception):
            return False
        return action == "continue" or action is None

    health = get_service_health()
    has_issues = False
    for svc in EXPECTED_SERVICES:
        info = health.get(svc) if '_error' not in health else None
        if info is None:
            status = f"{yellow}○ not running{reset}"
            has_issues = True
        elif info.get('health') == 'unhealthy':
            status = f"{red}✗ unhealthy{reset}"
            has_issues = True
        elif info.get('health') == 'starting':
            status = f"{yellow}○ starting{reset}"
        else:
            status = f"{green}● {info.get('state', 'running')}{reset}"
            if info.get('health') not in (None, 'healthy', 'starting'):
                status += f" ({info['health']})"
        print(f"  {svc:<24} {status}")

    print("─" * 44)
    print()

    if not has_issues:
        # All healthy — auto-continue, no prompt needed
        return True

    # Some services are unhealthy / missing — let user decide
    try:
        action = questionary.select(
            "  Some Docker services have issues. How would you like to proceed?",
            choices=[
                questionary.Choice(title="Continue to CLI", value="continue"),
                questionary.Choice(title="Abort", value="abort"),
            ],
            style=custom_style,
        ).ask()
    except (KeyboardInterrupt, Exception):
        return False

    return action == "continue" or action is None


def _get_k8s_node_containers() -> list[str]:
    """Return the Docker container names for all current Kubernetes nodes.

    For kind clusters the node container names match the kubectl node names
    (e.g. desktop-worker, desktop-worker2 …).  For minikube/docker-desktop
    the single node container is usually 'minikube' or managed by the OS.
    We fall back to `kind get nodes` if kubectl fails.
    """
    import subprocess
    try:
        r = subprocess.run(
            ["kubectl", "get", "nodes", "--no-headers",
             "-o", "custom-columns=NAME:.metadata.name"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode == 0:
            return [n.strip() for n in r.stdout.splitlines() if n.strip()]
    except Exception:
        pass
    # Fallback: kind get nodes
    try:
        r = subprocess.run(
            ["kind", "get", "nodes"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode == 0:
            return [n.strip() for n in r.stdout.splitlines() if n.strip()]
    except Exception:
        pass
    return []


def _image_on_node(container_name: str, image_name: str) -> bool:
    """Return True if *image_name* appears in crictl images on *container_name*."""
    import subprocess
    try:
        r = subprocess.run(
            ["docker", "exec", container_name, "crictl", "images",
             "--output", "json"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False
        import json as _json
        data = _json.loads(r.stdout)
        for img in data.get("images", []):
            for tag in img.get("repoTags", []):
                if image_name in tag:
                    return True
    except Exception:
        pass
    return False


_AGENT_IMAGE = "playwright-agent"
_AGENT_FULL  = "host.docker.internal:5050/playwright-agent:latest"


def _run_k8s_image_check() -> None:
    """Check whether the playwright-agent image is present on k8s worker nodes.

    Prints a status table (one row per node) and offers to run
    ``kind load docker-image`` inline if any node is missing the image.
    """
    import questionary
    import subprocess
    from simulation_service_tool.ui.styles import custom_style
    from simulation_service_tool.menus.image_pull import (
        _image_exists_locally,
        _get_kind_cluster_name,
        _kind_load_images,
    )

    green  = "\033[32m"
    yellow = "\033[33m"
    red    = "\033[31m"
    bold   = "\033[1m"
    dim    = "\033[2m"
    reset  = "\033[0m"

    print(f"{bold}K8s node image check{reset}")
    print("─" * 44)

    nodes = _get_k8s_node_containers()
    if not nodes:
        print(f"  {'Nodes':<28} {yellow}○ could not list nodes{reset}")
        print("─" * 44)
        print()
        return

    # Check all nodes in parallel
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(len(nodes), 8)) as pool:
        futures = {pool.submit(_image_on_node, n, _AGENT_IMAGE): n for n in nodes}
        node_status = {}
        for fut in futures:
            node_status[futures[fut]] = fut.result()

    missing_nodes = [n for n, ok in node_status.items() if not ok]
    present_nodes = [n for n, ok in node_status.items() if ok]

    # Only print worker nodes (skip control-plane in the per-row output to
    # keep the table short), but still check them.
    workers = [n for n in nodes if "worker" in n.lower() or "node" in n.lower()]
    control = [n for n in nodes if n not in workers]

    # Summary row for workers
    missing_workers = [n for n in workers if n in missing_nodes]
    present_workers = [n for n in workers if n in present_nodes]

    if workers:
        if not missing_workers:
            print(f"  {'playwright-agent':<28} {green}● loaded on all {len(workers)} worker node(s){reset}")
        else:
            print(f"  {'playwright-agent':<28} {red}✗ missing on {len(missing_workers)}/{len(workers)} worker node(s){reset}")
            for n in missing_workers[:4]:  # show at most 4 names
                print(f"  {dim}    · {n}{reset}")
            if len(missing_workers) > 4:
                print(f"  {dim}    … and {len(missing_workers)-4} more{reset}")
    else:
        # No workers found — show per-node
        for n in nodes[:5]:
            ok = node_status.get(n, False)
            sym = f"{green}●{reset}" if ok else f"{red}✗{reset}"
            print(f"  {dim}{sym} {n}{reset}")

    print("─" * 44)
    print()

    if not missing_nodes:
        return

    # Image is missing on some nodes — decide what to offer
    local_ok = _image_exists_locally(_AGENT_IMAGE)

    if not local_ok:
        print(f"  {yellow}[WARN]{reset} playwright-agent is not loaded on all nodes and is also not")
        print(f"  {dim}       found in the local Docker daemon.{reset}")
        print(f"  Build it first:  docker build -t playwright-agent .")
        print()
        return

    print(f"  {yellow}[WARN]{reset} playwright-agent exists locally but is {bold}not loaded{reset} into")
    print(f"         {len(missing_nodes)} cluster node(s). Pods will fail with ErrImagePull.")
    print()

    try:
        action = questionary.select(
            "Load playwright-agent into kind nodes now?",
            choices=[
                questionary.Choice(
                    title=f"Yes — kind load docker-image  ({len(missing_nodes)} node(s) missing)",
                    value="load",
                ),
                questionary.Choice(title="Skip (I'll fix it later)", value="skip"),
            ],
            style=custom_style,
        ).ask()
    except (KeyboardInterrupt, Exception):
        return

    if action != "load":
        return

    cluster = _get_kind_cluster_name()
    print(f"\n  Loading {_AGENT_IMAGE} into kind cluster '{cluster}'…\n")

    # Re-use _kind_load_images by passing a synthetic failing-pod-like entry
    fake_failing = [{"image": _AGENT_FULL}]
    results = _kind_load_images(fake_failing)

    ok_count = sum(1 for r in results if r["returncode"] == 0)
    if ok_count == len(results):
        print(f"\n  {green}✓{reset} Image loaded successfully into all nodes.\n")
    else:
        for r in results:
            if r["returncode"] != 0:
                print(f"\n  {red}[FAIL]{reset} {r.get('stderr','')[:120]}")
        print(f"\n  {yellow}[WARN]{reset} Some nodes may still be missing the image.\n")


def interactive_menu():
    # Step 1: K8s check — hard prerequisite, runs first.
    if not _early_k8s_check():
        return
    # Step 2: API check — simulation service prompt.
    if not _early_api_check():
        return
    # Step 3: Open dashboard in browser now that the service is confirmed up.
    _open_dashboard()
    # Step 4: Full parallel probe table (includes Docker API, runtime, etc.)
    ok, results = _run_startup_diagnostics()
    if not ok:
        return
    # Step 5: Docker Compose health check — show status, prompt to confirm.
    if not _run_docker_health_check():
        return
    # Step 6: K8s node image check — ensure playwright-agent is loaded.
    _run_k8s_image_check()
    # Step 7: Main menu
    welcome_menu()
