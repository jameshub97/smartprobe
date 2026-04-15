"""Kubernetes API connectivity detection, context scanning, and auto-recovery.

Extracted from the boot-time diagnostics in cli/main.py so the same logic
can be reused from smart_diagnostics, menus, and the CLI layer.
"""

import json
import os
import platform
import re
import socket
import subprocess
import sys
import time

from simulation_service_tool.services.smart_diagnostics import _docker_running

# ---------------------------------------------------------------------------
# Error patterns indicating an unreachable K8s API
# ---------------------------------------------------------------------------

_K8S_UNREACHABLE_PATTERNS = (
    "unable to connect",
    "connection refused",
    "tls handshake",
    "deadline exceeded",
    "no such host",
    "i/o timeout",
)


# ---------------------------------------------------------------------------
# Reachability probes
# ---------------------------------------------------------------------------

def k8s_reachable(timeout: float = 3.0) -> str:
    """Quick non-blocking check for the Kubernetes API.

    Returns one of:
      'reachable'         — kubectl cluster-info succeeded
      'unreachable'       — API not responding (or Docker not running)
      'kubectl not found' — kubectl binary missing
      'error: <detail>'   — unexpected failure
    """
    if not _docker_running():
        return "unreachable"
    try:
        result = subprocess.run(
            ["kubectl", "cluster-info", "--request-timeout=2s"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return "reachable"
        msg = (result.stderr or result.stdout or "").strip().lower()
        if any(pat in msg for pat in _K8S_UNREACHABLE_PATTERNS):
            return "unreachable"
        first_line = (result.stderr or result.stdout or "").strip().splitlines()
        short = first_line[0][:50] if first_line else "error"
        return f"error: {short}"
    except FileNotFoundError:
        return "kubectl not found"
    except subprocess.TimeoutExpired:
        return "unreachable"
    except Exception as exc:
        return f"error: {exc}"


def context_reachable(ctx: str, timeout: float = 2.0) -> bool:
    """Quick probe for a specific kubectl context."""
    try:
        r = subprocess.run(
            ["kubectl", "--context", ctx, "cluster-info", "--request-timeout=1s"],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0
    except Exception:
        return False


def probe_api_port(host: str, port: int, timeout: float = 0.5) -> str:
    """Return 'listening', 'eof', or 'closed'.

    'eof'       — TCP connects but the peer closes immediately (k8s API
                  process is crashing).
    'listening' — TCP connects and the peer sends at least 1 byte before the
                  probe times out (TLS handshake started → server is alive).
    'closed'    — TCP connection refused or timed out.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            try:
                data = s.recv(1)
                if data == b'':
                    return 'eof'
                return 'listening'
            except (socket.timeout, TimeoutError):
                return 'listening'
            except OSError:
                return 'eof'
    except OSError:
        return 'closed'


def k8s_stability_check(probes: int = 3, interval: float = 2.0,
                         print_fn=None) -> dict:
    """Probe K8s health multiple times to detect flaky clusters.

    Docker Desktop Kubernetes is known to appear healthy for a few seconds
    then crash with EOF.  A single reachability check can't catch this.

    Returns::

        {
            'stable': bool,        # True only if ALL probes passed
            'results': [str, ...], # per-probe: 'reachable', 'unreachable', ...
            'flaky': bool,         # True if results are mixed (healthy + failed)
        }
    """
    prt = print_fn  # None = silent
    results: list[str] = []
    for i in range(probes):
        r = k8s_reachable(timeout=4.0)
        results.append(r)
        if prt:
            marker = "\033[32m✓\033[0m" if r == "reachable" else "\033[31m✗\033[0m"
            prt(f"  {marker} probe {i + 1}/{probes}: {r}")
        if i < probes - 1:
            time.sleep(interval)

    all_ok = all(r == "reachable" for r in results)
    any_ok = any(r == "reachable" for r in results)
    return {
        'stable': all_ok,
        'results': results,
        'flaky': any_ok and not all_ok,
    }


# ---------------------------------------------------------------------------
# Context management
# ---------------------------------------------------------------------------

def get_available_contexts() -> tuple[str, list[str]]:
    """Return (current_context, [all_context_names]).  Both empty on failure."""
    from concurrent.futures import ThreadPoolExecutor

    def _current():
        return subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()

    def _all():
        return subprocess.run(
            ["kubectl", "config", "get-contexts", "-o", "name"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip().splitlines()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_cur = pool.submit(_current)
            f_all = pool.submit(_all)
            current = f_cur.result(timeout=5)
            all_ctx = f_all.result(timeout=5)
        return current, [c.strip() for c in all_ctx if c.strip()]
    except Exception:
        return "", []


def switch_context(ctx: str) -> bool:
    """Switch the active kubectl context.  Returns True on success."""
    try:
        r = subprocess.run(
            ["kubectl", "config", "use-context", ctx],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Minikube helpers
# ---------------------------------------------------------------------------

def is_minikube_installed() -> bool:
    """Check whether the minikube binary is available on PATH."""
    try:
        subprocess.run(["minikube", "version", "--short"],
                       capture_output=True, timeout=3)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def is_minikube_running() -> bool:
    """Check whether minikube has a running cluster."""
    try:
        r = subprocess.run(
            ["minikube", "status", "--format={{.Host}}"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and "running" in r.stdout.strip().lower()
    except Exception:
        return False


def try_minikube_start(print_fn=None) -> bool:
    """Attempt ``minikube start``; returns True if the cluster becomes reachable.

    *print_fn* defaults to ``print`` — override for testing or silent mode.
    """
    prt = print_fn or print
    if not is_minikube_installed():
        return False
    if is_minikube_running():
        # Already running — just ensure context is set
        switch_context("minikube")
        return context_reachable("minikube")

    bold, yellow, reset = "\033[1m", "\033[33m", "\033[0m"
    prt(f"\n  {yellow}→ minikube is installed but not running.{reset}")
    prt(f"  {bold}Starting minikube...{reset} (this may take ~60 s)")
    try:
        result = subprocess.run(
            ["minikube", "start"],
            stdout=None, stderr=None, timeout=180,
        )
        if result.returncode == 0 and context_reachable("minikube"):
            switch_context("minikube")
            prt(f"  {bold}done.{reset}")
            return True
        prt("  failed.")
        return False
    except subprocess.TimeoutExpired:
        prt("  timed out.")
        return False


# ---------------------------------------------------------------------------
# Kind helpers
# ---------------------------------------------------------------------------

def is_kind_installed() -> bool:
    """Check whether the kind binary is available on PATH."""
    try:
        subprocess.run(["kind", "version"], capture_output=True, timeout=3)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_kind_clusters() -> list[str]:
    """Return configured Kind cluster names."""
    try:
        r = subprocess.run(
            ["kind", "get", "clusters"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return []
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    except Exception:
        return []


def get_kind_containers() -> dict[str, dict]:
    """Return Kind Docker containers and their status.

    Returns ``{container_name: {'running': bool, 'status': str}}``.
    """
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--filter", "label=io.x-k8s.kind.cluster",
             "--format", "{{.Names}}:{{.Status}}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return {}
    except Exception:
        return {}

    containers: dict[str, dict] = {}
    for line in r.stdout.strip().splitlines():
        if ':' not in line:
            continue
        name, status = line.split(':', 1)
        name = name.strip()
        status = status.strip()
        # Validate container name (alphanumeric, dash, underscore only)
        if not re.match(r'^[a-zA-Z0-9_-]+$', name):
            continue
        containers[name] = {
            'running': status.startswith('Up'),
            'status': status,
        }
    return containers


def cluster_runtime_status() -> str:
    """Return a short label describing the local cluster runtime state."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_ctx = pool.submit(get_available_contexts)
        f_containers = pool.submit(get_kind_containers)
        f_clusters = pool.submit(get_kind_clusters)
        f_kind = pool.submit(is_kind_installed)

        try:
            current, _contexts = f_ctx.result(timeout=5)
        except Exception:
            current = ""
        try:
            kind_containers = f_containers.result(timeout=6)
        except Exception:
            kind_containers = {}
        try:
            kind_clusters = f_clusters.result(timeout=5)
        except Exception:
            kind_clusters = []
        try:
            kind_installed = f_kind.result(timeout=5)
        except Exception:
            kind_installed = False

    if any(state['running'] for state in kind_containers.values()):
        return 'kind running'
    if kind_containers:
        return 'kind stopped'
    if kind_clusters:
        return 'kind configured'
    if current == 'docker-desktop':
        return 'docker-desktop selected'
    if is_minikube_installed():
        return 'minikube running' if is_minikube_running() else 'minikube stopped'
    if kind_installed:
        return 'kind available'
    return 'no runtime'


def kubectl_probe_detail(timeout: float = 5.0) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for a ``kubectl cluster-info`` probe."""
    try:
        result = subprocess.run(
            ["kubectl", "cluster-info", "--request-timeout=3s"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return True, 'reachable'
        message = (result.stderr or result.stdout or '').strip()
        lines = [line.strip() for line in message.splitlines() if line.strip()]
        short = lines[-1] if lines else 'unreachable'
        return False, short[:120]
    except FileNotFoundError:
        return False, 'kubectl not found'
    except subprocess.TimeoutExpired:
        return False, 'context deadline exceeded'
    except Exception as exc:
        return False, str(exc)[:120]


def try_kind_restart(print_fn=None) -> bool:
    """Restart stopped Kind containers; returns True if cluster becomes reachable."""
    prt = print_fn or print
    containers = get_kind_containers()
    stopped = [n for n, s in containers.items() if not s['running']]
    if not stopped:
        return False

    bold, yellow, green, reset = "\033[1m", "\033[33m", "\033[32m", "\033[0m"
    prt(f"\n  {yellow}→ Found {len(stopped)} stopped Kind container(s): {', '.join(stopped)}{reset}")

    for name in stopped:
        prt(f"  {bold}Starting {name}...{reset}")
        try:
            r = subprocess.run(
                ["docker", "start", name],
                stdout=None, stderr=None, timeout=30,
            )
            if r.returncode == 0:
                prt(f"  {green}done.{reset}")
            else:
                prt("  failed.")
        except subprocess.TimeoutExpired:
            prt("  timed out.")

    # Give the API server a moment to come up
    prt(f"  {bold}Waiting for API server...{reset}")
    time.sleep(5)

    # Check if any kind context is now reachable
    current, contexts = get_available_contexts()
    for ctx in contexts:
        if 'kind' in ctx and context_reachable(ctx):
            switch_context(ctx)
            return True
    return False


def try_kind_reboot(print_fn=None) -> bool:
    """Restart running Kind containers; returns True if the API becomes reachable."""
    prt = print_fn or print
    containers = get_kind_containers()
    running = [n for n, s in containers.items() if s['running']]
    if not running:
        return False

    bold, yellow, green, reset = "\033[1m", "\033[33m", "\033[32m", "\033[0m"
    prt(f"\n  {yellow}→ Restarting Kind container(s): {', '.join(running)}{reset}")

    for name in running:
        prt(f"  {bold}Restarting {name}...{reset}")
        try:
            r = subprocess.run(
                ["docker", "restart", name],
                stdout=None, stderr=None, timeout=45,
            )
            if r.returncode == 0:
                prt(f"  {green}done.{reset}")
            else:
                prt("  failed.")
        except subprocess.TimeoutExpired:
            prt("  timed out.")

    prt(f"  {bold}Waiting for API server...{reset}")
    time.sleep(5)

    current, contexts = get_available_contexts()
    for ctx in contexts:
        if 'kind' in ctx and context_reachable(ctx):
            switch_context(ctx)
            return True
    return False


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------

def diagnose() -> dict:
    """Full diagnosis with actionable recommendations.

    Returns a dict with keys:
      status            — 'healthy' or 'unreachable'
      message           — human-readable summary
      running_cluster   — context name or None
      contexts_available — list of context names
      kind_containers   — output of get_kind_containers()
      minikube          — {'installed': bool, 'running': bool}
      recommendations   — output of build_recommendations()
      details           — output of collect_failure_details()
    """
    current, contexts = get_available_contexts()
    kind_containers = get_kind_containers()

    # Find a running cluster
    running_cluster = None
    if k8s_reachable() == 'reachable':
        running_cluster = current
    else:
        for ctx in contexts:
            if ctx != current and context_reachable(ctx):
                running_cluster = ctx
                break

    if running_cluster:
        return {
            'status': 'healthy',
            'message': f"Cluster '{running_cluster}' is running",
            'running_cluster': running_cluster,
            'contexts_available': contexts,
            'kind_containers': kind_containers,
            'minikube': {
                'installed': is_minikube_installed(),
                'running': is_minikube_running(),
            },
            'recommendations': [],
            'details': [],
        }

    recs = build_recommendations()
    details = collect_failure_details()
    return {
        'status': 'unreachable',
        'message': 'Kubernetes API unreachable',
        'running_cluster': None,
        'contexts_available': contexts,
        'kind_containers': kind_containers,
        'minikube': {
            'installed': is_minikube_installed(),
            'running': is_minikube_running(),
        },
        'recommendations': recs,
        'details': details,
    }


def _resolve_api_port() -> tuple[str, int]:
    """Return (host, port) for the current kubectl context's API server."""
    try:
        r = subprocess.run(
            ['kubectl', 'config', 'view', '--minify', '-o',
             'jsonpath={.clusters[0].cluster.server}'],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            from urllib.parse import urlparse
            parsed = urlparse(r.stdout.strip())
            return (parsed.hostname or '127.0.0.1', parsed.port or 6443)
    except Exception:
        pass
    return ('127.0.0.1', 6443)


def build_recommendations() -> list[dict]:
    """Build a prioritised list of actionable recovery recommendations.

    Each item: ``{'action': str, 'label': str, 'detail': str}``.
    """
    from concurrent.futures import ThreadPoolExecutor

    # Run all slow probes in parallel to avoid sequential timeout stacking.
    with ThreadPoolExecutor(max_workers=8) as pool:
        f_ctx = pool.submit(get_available_contexts)
        f_containers = pool.submit(get_kind_containers)
        f_clusters = pool.submit(get_kind_clusters)
        f_kind = pool.submit(is_kind_installed)
        f_mk_inst = pool.submit(is_minikube_installed)
        f_mk_run = pool.submit(is_minikube_running)
        f_port = pool.submit(_resolve_api_port)
        f_dd_k8s = pool.submit(_docker_desktop_k8s_enabled)

        try:
            current, _contexts = f_ctx.result(timeout=5)
        except Exception:
            current, _contexts = "", []
        try:
            kind_containers = f_containers.result(timeout=5)
        except Exception:
            kind_containers = {}
        try:
            kind_clusters = f_clusters.result(timeout=5)
        except Exception:
            kind_clusters = []
        try:
            kind_installed = f_kind.result(timeout=5)
        except Exception:
            kind_installed = False
        try:
            mk_installed = f_mk_inst.result(timeout=5)
        except Exception:
            mk_installed = False
        try:
            mk_running = f_mk_run.result(timeout=8)
        except Exception:
            mk_running = False
        try:
            api_host, api_port = f_port.result(timeout=5)
        except Exception:
            api_host, api_port = '127.0.0.1', 6443
        try:
            dd_k8s_enabled = f_dd_k8s.result(timeout=3)
        except Exception:
            dd_k8s_enabled = None

    # Detect if the API port is in a crash-loop (EOF = server process crashing)
    # or unstable (flickers between listening and eof/closed).
    port_state = probe_api_port(api_host, api_port)
    api_crashing = port_state == 'eof'

    recs: list[dict] = []
    active_kind = current.startswith('kind') if current else False
    active_minikube = current == 'minikube'
    active_dd = current == 'docker-desktop'
    no_active_context = not current
    kind_target = active_kind or no_active_context

    # Quick instability detection: 3 fast port probes over ~2s.
    # Docker Desktop K8s can appear healthy then crash seconds later.
    api_unstable = False
    if port_state == 'listening' and active_dd:
        states = [port_state]
        for _ in range(2):
            time.sleep(0.8)
            states.append(probe_api_port(api_host, api_port))
        api_unstable = len(set(states)) > 1  # mixed results = flaky

    # 0. Docker Desktop K8s is crashing (EOF) or unstable — needs a full
    #    reset or a switch to Kind.  This is a known failure mode where the
    #    embedded etcd/apiserver is corrupted and survives Docker restarts.
    if (api_crashing or api_unstable) and active_dd and sys.platform == 'darwin':
        if kind_installed:
            recs.append({
                'action': 'create_kind',
                'label': 'Switch to Kind cluster (more reliable)',
                'detail': 'kind create cluster',
            })
        if dd_k8s_enabled:
            recs.append({
                'action': 'reset_docker_k8s',
                'label': 'Reset Kubernetes cluster (disable + re-enable)',
                'detail': 'Docker Desktop → Settings → Kubernetes → toggle off/on',
            })

    # 1. Stopped Kind containers — cheapest fix (just docker start)
    stopped_kind = [n for n, s in kind_containers.items() if not s['running']]
    running_kind = [n for n, s in kind_containers.items() if s['running']]
    if stopped_kind and kind_target:
        recs.append({
            'action': 'start_kind',
            'label': f"Start Kind container ({stopped_kind[0]})",
            'detail': f"docker start {stopped_kind[0]}",
        })

    # 2. Running Kind container with an unresponsive API — restart it.
    if running_kind and kind_target:
        recs.append({
            'action': 'restart_kind',
            'label': f"Restart Kind container ({running_kind[0]})",
            'detail': f"docker restart {running_kind[0]}",
        })

    # 2b. Kind containers or clusters exist but API is unreachable — delete + recreate.
    if (running_kind or kind_clusters) and kind_target and kind_installed:
        recs.append({
            'action': 'recreate_kind',
            'label': 'Delete & recreate Kind cluster',
            'detail': 'kind delete cluster && kind create cluster',
        })

    # 3. No Kind cluster and no containers — fresh create
    if kind_installed and not kind_clusters and not kind_containers and kind_target:
        recs.append({
            'action': 'create_kind',
            'label': 'Create new Kind cluster',
            'detail': 'kind create cluster',
        })

    # 4. Minikube installed but not running — only when minikube is active,
    # or there is no active context to prefer over it.
    if mk_installed and not mk_running and (active_minikube or no_active_context):
        recs.append({
            'action': 'start_minikube',
            'label': 'Start minikube cluster',
            'detail': 'minikube start',
        })

    # 5. Enable Docker Desktop's built-in Kubernetes (macOS only).
    #    Offered when no cluster is running and Docker Desktop K8s is disabled.
    if sys.platform == 'darwin' and not running_kind and not mk_running:
        dd_k8s = _docker_desktop_k8s_enabled()
        if dd_k8s is False:
            recs.append({
                'action': 'enable_docker_k8s',
                'label': 'Enable Docker Desktop Kubernetes',
                'detail': 'Toggle kubernetesEnabled in Docker Desktop settings & restart',
            })

    # 6. Restart Docker Desktop — nuclear option when Docker itself is degraded.
    #    Always offered as the last resort on macOS.
    if sys.platform == 'darwin':
        recs.append({
            'action': 'restart_docker_desktop',
            'label': 'Restart Docker Desktop',
            'detail': 'killall Docker && open /Applications/Docker.app',
        })

    return recs


def _nuke_kind_cluster(prt=None):
    """Thoroughly remove all Kind containers and cluster metadata.

    Uses Docker label filtering (io.x-k8s.kind.cluster) to catch every
    Kind-managed container, then runs ``kind delete cluster`` for metadata
    cleanup, and finally verifies nothing is left.
    """
    prt = prt or print
    bold, dim, reset = "\033[1m", "\033[2m", "\033[0m"

    # 1. Find ALL Kind containers by label (more reliable than name filter)
    container_ids = []
    try:
        r = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "label=io.x-k8s.kind.cluster"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            container_ids = r.stdout.strip().split()
    except Exception:
        pass

    # Also check by name pattern as a fallback
    try:
        r = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=kind-"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            for cid in r.stdout.strip().split():
                if cid not in container_ids:
                    container_ids.append(cid)
    except Exception:
        pass

    # 2. Force-remove all found containers
    if container_ids:
        prt(f"\n  {bold}Removing Kind container(s)...{reset}")
        try:
            subprocess.run(
                ["docker", "rm", "-f"] + container_ids,
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            pass
        # Brief pause for Docker to fully release resources
        time.sleep(1)

    # 3. Clean up Kind's internal state
    prt(f"  {bold}Cleaning up Kind cluster metadata...{reset}")
    try:
        subprocess.run(
            ["kind", "delete", "cluster"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        pass

    # 4. Verify nothing remains — catch stragglers
    try:
        r = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "label=io.x-k8s.kind.cluster"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            leftovers = r.stdout.strip().split()
            prt(f"  {dim}Removing {len(leftovers)} leftover container(s)...{reset}")
            subprocess.run(
                ["docker", "rm", "-f"] + leftovers,
                capture_output=True, text=True, timeout=15,
            )
            time.sleep(1)
    except Exception:
        pass


def apply_recommendation(action: str, print_fn=None) -> bool:
    """Execute a single remediation action.  Returns True on success."""
    prt = print_fn or print
    bold, green, red, dim, reset = "\033[1m", "\033[32m", "\033[31m", "\033[2m", "\033[0m"

    if action == 'start_kind':
        return try_kind_restart(print_fn=prt)

    if action == 'restart_kind':
        return try_kind_reboot(print_fn=prt)

    if action == 'start_minikube':
        return try_minikube_start(print_fn=prt)

    if action == 'recreate_kind':
        _nuke_kind_cluster(prt)
        prt(f"  {bold}Creating new Kind cluster...{reset}")
        try:
            r = subprocess.run(
                ["kind", "create", "cluster"],
                stdout=None, stderr=None, timeout=180,
            )
            if r.returncode == 0:
                time.sleep(3)
                if context_reachable("kind-kind"):
                    switch_context("kind-kind")
                    prt(f"  {green}done.{reset}")
                    return True
            prt(f"  {red}failed.{reset}")
            return False
        except subprocess.TimeoutExpired:
            prt(f"  {red}timed out.{reset}")
            return False

    if action == 'create_kind':
        _nuke_kind_cluster(prt)
        prt(f"  {bold}Creating Kind cluster...{reset}")
        try:
            r = subprocess.run(
                ["kind", "create", "cluster"],
                stdout=None, stderr=None, timeout=180,
            )
            if r.returncode == 0:
                time.sleep(3)
                if context_reachable("kind-kind"):
                    switch_context("kind-kind")
                    prt(f"  {green}done.{reset}")
                    return True
            prt(f"  {red}failed.{reset}")
            return False
        except subprocess.TimeoutExpired:
            prt(f"  {red}timed out.{reset}")
            return False

    if action == 'restart_docker_desktop':
        return _restart_docker_desktop(prt)

    if action == 'enable_docker_k8s':
        return _enable_docker_desktop_k8s(prt)

    if action == 'reset_docker_k8s':
        return _reset_docker_desktop_k8s(prt)

    return False


def _restart_docker_desktop(prt=None) -> bool:
    """Kill Docker Desktop, wait for it to restart, then verify.

    macOS only. Returns True if Docker is responsive after restart.
    """
    prt = prt or print
    bold, green, red, yellow, dim, reset = (
        "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
    )

    prt(f"\n  {yellow}→ Restarting Docker Desktop...{reset}")
    prt(f"  {dim}This will briefly stop all containers.{reset}")

    # 1. Kill Docker Desktop
    prt(f"  {bold}Stopping Docker Desktop...{reset}")
    try:
        subprocess.run(["killall", "Docker"], capture_output=True, timeout=10)
    except Exception:
        pass
    # Wait for Docker daemon to actually stop
    time.sleep(3)

    # 2. Re-launch Docker Desktop
    prt(f"  {bold}Starting Docker Desktop...{reset}")
    try:
        subprocess.run(["open", "/Applications/Docker.app"],
                       capture_output=True, timeout=10)
    except Exception:
        prt(f"  {red}Could not launch Docker Desktop.{reset}")
        return False

    # 3. Wait for Docker daemon to become responsive (poll every 3s, up to 60s)
    prt(f"  {bold}Waiting for Docker daemon...{reset}", end="", flush=True)
    for i in range(20):
        time.sleep(3)
        try:
            r = subprocess.run(
                ["docker", "info"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                prt(f" {green}ready.{reset}")
                # 4. Brief extra wait for container runtime to stabilize
                time.sleep(2)
                return True
        except Exception:
            pass
        prt(".", end="", flush=True)

    prt(f" {red}timed out.{reset}")
    return False


# ---------------------------------------------------------------------------
# Docker Desktop Kubernetes helpers
# ---------------------------------------------------------------------------

_DD_SETTINGS = os.path.expanduser(
    "~/Library/Group Containers/group.com.docker/settings-store.json"
)


def _docker_desktop_k8s_enabled() -> bool | None:
    """Check if Docker Desktop's built-in Kubernetes is enabled.

    Returns True/False, or None if the settings file can't be read
    (e.g. not macOS, Docker Desktop not installed).
    """
    try:
        with open(_DD_SETTINGS) as f:
            settings = json.load(f)
        return bool(settings.get("KubernetesEnabled", False))
    except Exception:
        return None


def _enable_docker_desktop_k8s(prt=None) -> bool:
    """Enable Docker Desktop Kubernetes and restart Docker Desktop.

    Writes ``KubernetesEnabled: true`` to the Docker Desktop settings
    file, then restarts Docker Desktop so it picks up the change.
    Returns True once the K8s API server is reachable.
    """
    prt = prt or print
    bold, green, red, yellow, dim, reset = (
        "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m",
    )

    prt(f"\n  {yellow}→ Enabling Docker Desktop Kubernetes...{reset}")

    # 1. Read current settings
    try:
        with open(_DD_SETTINGS) as f:
            settings = json.load(f)
    except Exception as exc:
        prt(f"  {red}Cannot read Docker Desktop settings: {exc}{reset}")
        return False

    if settings.get("KubernetesEnabled"):
        prt(f"  {dim}Kubernetes is already enabled in Docker Desktop settings.{reset}")
    else:
        # 2. Flip the flag
        settings["KubernetesEnabled"] = True
        try:
            with open(_DD_SETTINGS, "w") as f:
                json.dump(settings, f, indent=2)
            prt(f"  {bold}Updated settings → KubernetesEnabled: true{reset}")
        except Exception as exc:
            prt(f"  {red}Cannot write Docker Desktop settings: {exc}{reset}")
            return False

    # 3. Restart Docker Desktop so it picks up the new setting
    prt(f"  {bold}Restarting Docker Desktop to apply...{reset}")
    try:
        subprocess.run(["killall", "Docker"], capture_output=True, timeout=10)
    except Exception:
        pass
    time.sleep(3)

    try:
        subprocess.run(["open", "/Applications/Docker.app"],
                       capture_output=True, timeout=10)
    except Exception:
        prt(f"  {red}Could not launch Docker Desktop.{reset}")
        return False

    # 4. Wait for Docker daemon first
    prt(f"  {bold}Waiting for Docker daemon...{reset}", end="", flush=True)
    docker_ready = False
    for _ in range(20):
        time.sleep(3)
        try:
            r = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                prt(f" {green}ready.{reset}")
                docker_ready = True
                break
        except Exception:
            pass
        prt(".", end="", flush=True)

    if not docker_ready:
        prt(f" {red}timed out waiting for Docker.{reset}")
        return False

    # 5. Wait for the K8s API server to come up (can take 30-90s)
    prt(f"  {bold}Waiting for Kubernetes API...{reset}", end="", flush=True)
    for _ in range(30):
        time.sleep(3)
        try:
            r = subprocess.run(
                ["kubectl", "get", "nodes"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                prt(f" {green}ready.{reset}")
                # Switch context to docker-desktop if available
                try:
                    switch_context("docker-desktop")
                    prt(f"  {dim}Switched context to docker-desktop.{reset}")
                except Exception:
                    pass
                return True
        except Exception:
            pass
        prt(".", end="", flush=True)

    prt(f" {red}timed out waiting for Kubernetes.{reset}")
    return False


def _reset_docker_desktop_k8s(prt=None) -> bool:
    """Reset Docker Desktop Kubernetes by disabling then re-enabling it.

    This fixes the known failure mode where the embedded K8s cluster gets
    into a corrupted state (EOF on API port) that survives Docker restarts.

    Steps:
      1. Disable KubernetesEnabled in settings-store.json
      2. Restart Docker Desktop so it tears down the broken cluster
      3. Wait for Docker daemon
      4. Re-enable KubernetesEnabled
      5. Restart Docker Desktop again so it provisions a fresh cluster
      6. Wait for K8s API
    """
    prt = prt or print
    bold, green, red, yellow, dim, reset = (
        "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m",
    )

    prt(f"\n  {yellow}→ Resetting Docker Desktop Kubernetes cluster...{reset}")
    prt(f"  {dim}This disables K8s, restarts Docker, then re-enables K8s.{reset}")

    # 1. Read current settings
    try:
        with open(_DD_SETTINGS) as f:
            settings = json.load(f)
    except Exception as exc:
        prt(f"  {red}Cannot read Docker Desktop settings: {exc}{reset}")
        return False

    # 2. Disable Kubernetes
    prt(f"  {bold}Disabling Kubernetes...{reset}")
    settings["KubernetesEnabled"] = False
    try:
        with open(_DD_SETTINGS, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception as exc:
        prt(f"  {red}Cannot write Docker Desktop settings: {exc}{reset}")
        return False

    # 3. Restart Docker Desktop to tear down broken cluster
    prt(f"  {bold}Restarting Docker Desktop (teardown)...{reset}")
    try:
        subprocess.run(["killall", "Docker"], capture_output=True, timeout=10)
    except Exception:
        pass
    time.sleep(3)

    try:
        subprocess.run(["open", "/Applications/Docker.app"],
                       capture_output=True, timeout=10)
    except Exception:
        prt(f"  {red}Could not launch Docker Desktop.{reset}")
        return False

    # 4. Wait for Docker daemon
    prt(f"  {bold}Waiting for Docker daemon...{reset}", end="", flush=True)
    docker_ready = False
    for _ in range(20):
        time.sleep(3)
        try:
            r = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                prt(f" {green}ready.{reset}")
                docker_ready = True
                break
        except Exception:
            pass
        prt(".", end="", flush=True)

    if not docker_ready:
        prt(f" {red}timed out waiting for Docker.{reset}")
        return False

    # 5. Re-enable Kubernetes
    prt(f"  {bold}Re-enabling Kubernetes...{reset}")
    try:
        with open(_DD_SETTINGS) as f:
            settings = json.load(f)
    except Exception:
        pass  # Use the in-memory copy
    settings["KubernetesEnabled"] = True
    try:
        with open(_DD_SETTINGS, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception as exc:
        prt(f"  {red}Cannot write Docker Desktop settings: {exc}{reset}")
        return False

    # 6. Restart Docker Desktop again to provision fresh cluster
    prt(f"  {bold}Restarting Docker Desktop (fresh cluster)...{reset}")
    try:
        subprocess.run(["killall", "Docker"], capture_output=True, timeout=10)
    except Exception:
        pass
    time.sleep(3)

    try:
        subprocess.run(["open", "/Applications/Docker.app"],
                       capture_output=True, timeout=10)
    except Exception:
        prt(f"  {red}Could not launch Docker Desktop.{reset}")
        return False

    # 7. Wait for Docker daemon again
    prt(f"  {bold}Waiting for Docker daemon...{reset}", end="", flush=True)
    docker_ready = False
    for _ in range(20):
        time.sleep(3)
        try:
            r = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                prt(f" {green}ready.{reset}")
                docker_ready = True
                break
        except Exception:
            pass
        prt(".", end="", flush=True)

    if not docker_ready:
        prt(f" {red}timed out waiting for Docker.{reset}")
        return False

    # 8. Wait for K8s API (fresh cluster takes 30-120s)
    prt(f"  {bold}Waiting for Kubernetes API (fresh cluster)...{reset}", end="", flush=True)
    for _ in range(40):
        time.sleep(3)
        try:
            r = subprocess.run(
                ["kubectl", "get", "nodes", "--request-timeout=3s"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                prt(f" {green}ready.{reset}")
                try:
                    switch_context("docker-desktop")
                    prt(f"  {dim}Switched context to docker-desktop.{reset}")
                except Exception:
                    pass
                return True
        except Exception:
            pass
        prt(".", end="", flush=True)

    prt(f" {red}timed out waiting for Kubernetes.{reset}")
    return False


# ---------------------------------------------------------------------------
# Failure diagnostics
# ---------------------------------------------------------------------------

def collect_failure_details() -> list[tuple[str, str, bool]]:
    """Return a list of (label, value, is_problem) tuples for K8s failure diagnosis."""
    from concurrent.futures import ThreadPoolExecutor

    # Phase 1: run all independent probes in parallel
    def _api_server_url():
        try:
            r = subprocess.run(
                ['kubectl', 'config', 'view', '--minify', '-o',
                 'jsonpath={.clusters[0].cluster.server}'],
                capture_output=True, text=True, timeout=3,
            )
            return r.stdout.strip() if r.returncode == 0 else ''
        except Exception:
            return ''

    def _memory_info():
        try:
            if platform.system() == 'Darwin':
                r = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=2)
                if r.returncode == 0:
                    free_pages = 0
                    for line in r.stdout.splitlines():
                        if 'Pages free' in line or 'Pages inactive' in line:
                            free_pages += int(''.join(filter(str.isdigit, line.split('.')[0])))
                    return round(free_pages * 4096 / 1024 ** 3, 1)
            else:
                with open('/proc/meminfo') as f:
                    for line in f:
                        if line.startswith('MemAvailable'):
                            return round(int(line.split()[1]) / 1024 / 1024, 1)
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        f_ctx = pool.submit(get_available_contexts)
        f_docker = pool.submit(_docker_running)
        f_kubectl = pool.submit(kubectl_probe_detail)
        f_url = pool.submit(_api_server_url)
        f_clusters = pool.submit(get_kind_clusters)
        f_containers = pool.submit(get_kind_containers)
        f_mk_inst = pool.submit(is_minikube_installed)
        f_mem = pool.submit(_memory_info)

        try:
            current, _contexts = f_ctx.result(timeout=6)
        except Exception:
            current, _contexts = '', []
        try:
            docker_ok = f_docker.result(timeout=5)
        except Exception:
            docker_ok = False
        try:
            kubectl_ok, kubectl_detail = f_kubectl.result(timeout=8)
        except Exception:
            kubectl_ok, kubectl_detail = False, 'probe timed out'
        try:
            server_url = f_url.result(timeout=5)
        except Exception:
            server_url = ''
        try:
            clusters = f_clusters.result(timeout=5)
        except Exception:
            clusters = []
        try:
            kind_containers = f_containers.result(timeout=6)
        except Exception:
            kind_containers = {}
        try:
            mk_installed = f_mk_inst.result(timeout=5)
        except Exception:
            mk_installed = False
        try:
            free_gb = f_mem.result(timeout=4)
        except Exception:
            free_gb = None

    active_kind = current.startswith('kind') if current else False
    active_minikube = current == 'minikube'

    # Phase 2: assemble results + one dependent probe (port check needs URL)
    details: list[tuple[str, str, bool]] = []

    details.append(('Docker runtime', 'reachable' if docker_ok else 'unreachable', not docker_ok))

    if current:
        details.append(('kubectl context', current, False))

    details.append(('kubectl probe', kubectl_detail, not kubectl_ok))

    # Docker Desktop Kubernetes toggle
    settings_path = os.path.expanduser('~/.docker/desktop/settings.json')
    if current == 'docker-desktop' and os.path.exists(settings_path):
        try:
            with open(settings_path) as f:
                s = json.load(f)
            k8s = s.get('kubernetesEnabled',
                        s.get('kubernetes', {}).get('enabled')
                        if isinstance(s.get('kubernetes'), dict) else None)
            if k8s is not None:
                val = 'enabled' if k8s else 'DISABLED  ← Settings → Kubernetes → Enable'
                details.append(('Docker Desktop k8s', val, not k8s))
        except Exception:
            pass

    # API server URL + port probe
    api_host = '127.0.0.1'
    api_port = 6443
    if server_url:
        details.append(('API server URL', server_url, False))
        try:
            from urllib.parse import urlparse
            parsed = urlparse(server_url)
            if parsed.hostname:
                api_host = parsed.hostname
            if parsed.port:
                api_port = parsed.port
        except Exception:
            pass

    port_label = f'Port {api_port} (k8s API)'
    port_state = probe_api_port(api_host, api_port)
    if port_state == 'eof':
        if active_kind:
            hint = 'CRASHING (EOF)  ← delete & recreate Kind cluster'
        elif active_dd:
            hint = 'CRASHING (EOF)  ← reset K8s or switch to Kind'
        else:
            hint = 'CRASHING (EOF)  ← restart Docker Desktop'
        details.append((port_label, hint, True))
    elif port_state == 'listening':
        if kubectl_ok:
            details.append((port_label, 'listening', False))
        else:
            details.append((port_label, f'listening, but API unavailable ({kubectl_detail})', True))
    else:
        details.append((port_label, 'not listening', True))

    # Kind clusters
    try:
        val = ', '.join(clusters) if clusters else 'none'
        details.append(('kind clusters', val, len(clusters) == 0 and active_kind))
    except Exception:
        pass

    # Kind containers
    if kind_containers:
        stopped = [n for n, s in kind_containers.items() if not s['running']]
        running = [n for n, s in kind_containers.items() if s['running']]
        if stopped:
            details.append(('kind containers',
                            f"{len(stopped)} stopped: {', '.join(stopped)}",
                            True))
        elif running:
            kind_problem = active_kind and not kubectl_ok
            suffix = '  ← control plane running, API not responding' if kind_problem else ''
            details.append(('kind containers',
                            f"{len(running)} running: {', '.join(running)}{suffix}",
                            kind_problem))

    # Available memory
    if free_gb is not None:
        kind_available = bool(kind_containers or clusters)
        minikube_relevant = active_minikube or (not current and not kind_available)
        low = free_gb < 2.0 and minikube_relevant
        details.append(('Available memory',
                        f'{free_gb} GB free{"  ← minikube needs ≥2 GB" if low else ""}',
                        low))

    # minikube status
    if mk_installed:
        running = is_minikube_running()
        if active_minikube or not current:
            val = 'running' if running else 'installed but not running'
            details.append(('minikube', val, not running))
        else:
            details.append(('minikube', f'installed, inactive for context {current}', False))
    else:
        details.append(('minikube', 'not installed', False))

    return details


def format_failure_details(details: list[tuple[str, str, bool]]) -> str:
    """Build a formatted multi-line string from failure details."""
    if not details:
        return ""
    dim, red, reset = "\033[2m", "\033[31m", "\033[0m"
    lines = ["  Diagnostic details:"]
    for label, value, is_problem in details:
        marker = f"{red}✗{reset}" if is_problem else f"{dim}·{reset}"
        colour = red if is_problem else dim
        lines.append(f"  {marker} {label:<28} {colour}{value}{reset}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrated auto-recovery
# ---------------------------------------------------------------------------

def attempt_recovery(print_fn=None) -> bool:
    """Try to automatically recover a working Kubernetes context.

    Strategy (in order):
      1. Try all available contexts for an already-running cluster (fast).
      2. Restart stopped Kind containers if any exist.
      3. Attempt ``minikube start`` if minikube is installed but not running.

    *print_fn* defaults to ``print`` — override for testing or silent mode.
    Returns True if a working context was found/started and made current.
    """
    prt = print_fn or print
    bold = "\033[1m"
    dim = "\033[2m"
    yellow = "\033[33m"
    green = "\033[32m"
    reset = "\033[0m"

    current, contexts = get_available_contexts()
    if not contexts:
        return False

    # --- Pass 1: probe every context for an already-running cluster ----------
    prt(f"\n  {yellow}→ Kubernetes API unreachable on context '{current}'.{reset}")
    prt(f"  {dim}Scanning {len(contexts)} available context(s) for a live cluster...{reset}")

    from concurrent.futures import ThreadPoolExecutor
    other_contexts = [ctx for ctx in contexts if ctx != current]
    if other_contexts:
        with ThreadPoolExecutor(max_workers=min(len(other_contexts), 5)) as pool:
            futures = {pool.submit(context_reachable, ctx): ctx for ctx in other_contexts}
            for future in futures:
                ctx = futures[future]
                prt(f"    checking {ctx}... ", end="")
                try:
                    reachable = future.result(timeout=5)
                except Exception:
                    reachable = False
                if reachable:
                    switch_context(ctx)
                    prt(f"{green}reachable — switching.{reset}")
                    return True
                prt(f"{dim}unreachable{reset}")

    # --- Pass 2+: apply the same context-aware recommendations shown in the UI
    for rec in build_recommendations():
        if apply_recommendation(rec['action'], print_fn=prt):
            return True

    return False


def diagnose_and_recover(print_fn=None) -> bool:
    """Full diagnostic + recovery flow suitable for menus/smart_diagnostics.

    Returns True if the K8s API is now reachable after recovery.
    """
    prt = print_fn or print
    bold, green, red, dim, reset = "\033[1m", "\033[32m", "\033[31m", "\033[2m", "\033[0m"

    status = k8s_reachable()
    if status == "reachable":
        return True

    if not _docker_running():
        prt(f"\n  {red}{bold}Failed to connect to Docker API.{reset}")
        prt(f"  {dim}Check Docker Desktop is running and Kubernetes is enabled in Settings.{reset}")
        return False

    # Attempt auto-recovery
    if attempt_recovery(print_fn=prt):
        # Re-verify
        if k8s_reachable() == "reachable":
            prt(f"\n  {green}{bold}✓ Kubernetes API now reachable.{reset} Continuing...\n")
            return True

    # Recovery failed — show diagnostic details
    prt(f"\n  {red}{bold}Kubernetes API is unreachable — recovery failed.{reset}")
    details = collect_failure_details()
    if details:
        prt(format_failure_details(details))
    prt(f"\n  {bold}Manual options:{reset}")
    prt(f"  {dim}· Docker Desktop: Settings → Kubernetes → Enable Kubernetes → Apply & Restart.{reset}")
    prt(f"  {dim}· minikube: run  minikube start{reset}")
    prt(f"  {dim}· kind: run  kind create cluster{reset}")
    prt(f"  {dim}· Check contexts: kubectl config get-contexts{reset}\n")
    return False
