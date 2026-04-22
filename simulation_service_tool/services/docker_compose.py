"""Docker Compose lifecycle management for the simulation platform."""

import json
import os
import subprocess


def _find_compose_file() -> str:
    """Locate docker-compose.yml by walking up from the package directory.

    Search order:
      1. $SIMULATION_COMPOSE_FILE (explicit override)
      2. Project root (three levels up from this file: services/ → package/ → repo/)
      3. Walk upward from cwd until we find docker-compose.yml or hit /
    """
    env = os.environ.get('SIMULATION_COMPOSE_FILE')
    if env:
        return env

    # Canonical location: repo root relative to this source file
    package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    canonical = os.path.join(package_root, 'docker-compose.yml')
    if os.path.isfile(canonical):
        return canonical

    # Fallback: walk up from cwd
    search = os.path.abspath(os.getcwd())
    while True:
        candidate = os.path.join(search, 'docker-compose.yml')
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(search)
        if parent == search:
            break
        search = parent

    # Nothing found — return the canonical path so callers get a
    # predictable (non-existent) path for error messages.
    return canonical


_COMPOSE_FILE = _find_compose_file()

EXPECTED_SERVICES = ('postgres', 'backend1', 'backend2', 'simulation', 'nginx', 'frontend')


def compose_file_path() -> str:
    """Return the resolved docker-compose.yml path (may not exist)."""
    return _COMPOSE_FILE


def _compose_cmd():
    """Return base docker compose command list."""
    return ['docker', 'compose', '-f', _COMPOSE_FILE]


def compose_file_exists() -> bool:
    return os.path.isfile(_COMPOSE_FILE)


def is_docker_available() -> bool:
    """Check if the docker CLI and daemon are reachable."""
    try:
        result = subprocess.run(
            ['docker', 'info'],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def is_compose_running() -> bool:
    """Return True if any compose services are currently running."""
    try:
        result = subprocess.run(
            _compose_cmd() + ['ps', '--format', 'json', '-q'],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def up(build=False, detach=True):
    """Start the Docker Compose stack.

    Returns a dict with 'success' bool and optional 'error' string.
    """
    cmd = _compose_cmd() + ['up']
    if build:
        cmd.append('--build')
    if detach:
        cmd.append('-d')
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            return {'success': True}
        return {'success': False, 'error': result.stderr.strip() or 'docker compose up failed'}
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': 'docker compose up timed out after 5 minutes'}
    except FileNotFoundError:
        return {'success': False, 'error': 'docker command not found'}
    except OSError as exc:
        return {'success': False, 'error': str(exc)}


def up_streaming(build=False, detach=True):
    """Start the Docker Compose stack, streaming output to stdout/stderr live.

    Unlike ``up()``, this does not capture output so the user sees build
    progress in real time.  Returns a dict with 'success' bool and an
    optional 'error' string (last 2 KB of stderr on failure).
    """
    import io
    cmd = _compose_cmd() + ['up']
    if build:
        cmd.append('--build')
    if detach:
        cmd.append('-d')
    try:
        # Capture stderr for error detection while letting stdout stream live.
        proc = subprocess.Popen(
            cmd,
            stdout=None,    # inherit — streams to terminal
            stderr=subprocess.PIPE,
            text=True,
        )
        stderr_buf = io.StringIO()
        # Read stderr line-by-line so we can both print it live and capture it.
        for line in proc.stderr:
            print(line, end='', flush=True)
            stderr_buf.write(line)
        proc.wait(timeout=300)
        if proc.returncode == 0:
            return {'success': True}
        err = stderr_buf.getvalue().strip()
        return {'success': False, 'error': err or 'docker compose up failed'}
    except subprocess.TimeoutExpired:
        proc.kill()
        return {'success': False, 'error': 'docker compose up timed out after 5 minutes'}
    except FileNotFoundError:
        return {'success': False, 'error': 'docker command not found'}
    except OSError as exc:
        return {'success': False, 'error': str(exc)}


def down(volumes=False):
    """Stop the Docker Compose stack.

    Returns a dict with 'success' bool and optional 'error' string.
    """
    cmd = _compose_cmd() + ['down']
    if volumes:
        cmd.append('-v')
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            return {'success': True}
        return {'success': False, 'error': result.stderr.strip() or 'docker compose down failed'}
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': 'docker compose down timed out'}
    except FileNotFoundError:
        return {'success': False, 'error': 'docker command not found'}
    except OSError as exc:
        return {'success': False, 'error': str(exc)}


def get_service_health():
    """Return per-service health status from docker compose ps.

    Returns a dict mapping service names to status dicts:
      { 'service': { 'state': str, 'health': str, 'running': bool } }
    """
    try:
        result = subprocess.run(
            _compose_cmd() + ['ps', '--format', 'json'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {'_error': result.stderr.strip() or 'docker compose ps failed'}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {'_error': str(exc)}

    services = {}
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = entry.get('Service') or entry.get('Name', '')
        state = entry.get('State', 'unknown')
        health = entry.get('Health', '')
        services[name] = {
            'state': state,
            'health': health,
            'running': state == 'running',
        }
    return services


def get_logs(service=None, tail=50):
    """Fetch logs from compose services.

    Returns a dict with 'output' string or 'error' string.
    """
    cmd = _compose_cmd() + ['logs', '--tail', str(int(tail)), '--no-color']
    if service:
        cmd.append(str(service))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return {'output': result.stdout}
    except subprocess.TimeoutExpired:
        return {'error': 'docker compose logs timed out'}
    except (FileNotFoundError, OSError) as exc:
        return {'error': str(exc)}


def test_endpoints():
    """Test health endpoints exposed by the compose stack.

    Returns a list of dicts: { 'name': str, 'url': str, 'status': int|str, 'healthy': bool }
    """
    import urllib.request
    import urllib.error
    from concurrent.futures import ThreadPoolExecutor

    endpoints = [
        ('backend (direct)', 'http://localhost:5001/health'),
        ('simulation (direct)', 'http://localhost:5002/health'),
        ('nginx (load balancer)', 'http://localhost:8080/'),
    ]

    def _probe(name, url):
        try:
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=5) as resp:
                return {'name': name, 'url': url, 'status': resp.status, 'healthy': resp.status == 200}
        except urllib.error.HTTPError as exc:
            return {'name': name, 'url': url, 'status': exc.code, 'healthy': False}
        except Exception as exc:
            return {'name': name, 'url': url, 'status': str(exc), 'healthy': False}

    with ThreadPoolExecutor(max_workers=len(endpoints)) as pool:
        futures = [pool.submit(_probe, name, url) for name, url in endpoints]
        return [f.result(timeout=10) for f in futures]
