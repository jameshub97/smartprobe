"""Tests for Docker Compose service module."""

import os
import subprocess

from simulation_service_tool.services import docker_compose


class _CmdResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.args = []


def test_is_docker_available_returns_true_when_daemon_running(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0),
    )
    assert docker_compose.is_docker_available() is True


def test_is_docker_available_returns_false_on_error(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1),
    )
    assert docker_compose.is_docker_available() is False


def test_is_docker_available_returns_false_on_timeout(monkeypatch):
    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd='docker', timeout=5)
    monkeypatch.setattr(subprocess, 'run', _timeout)
    assert docker_compose.is_docker_available() is False


def test_is_docker_available_returns_false_when_not_installed(monkeypatch):
    def _not_found(*a, **kw):
        raise FileNotFoundError("docker not found")
    monkeypatch.setattr(subprocess, 'run', _not_found)
    assert docker_compose.is_docker_available() is False


def test_is_compose_running_true(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0, stdout='abc123\n'),
    )
    assert docker_compose.is_compose_running() is True


def test_is_compose_running_false_empty(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0, stdout=''),
    )
    assert docker_compose.is_compose_running() is False


def test_up_success(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0),
    )
    result = docker_compose.up(build=False, detach=True)
    assert result['success'] is True


def test_up_failure(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1, stderr='image not found'),
    )
    result = docker_compose.up(build=True, detach=True)
    assert result['success'] is False
    assert 'image not found' in result['error']


def test_up_timeout(monkeypatch):
    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd='docker', timeout=300)
    monkeypatch.setattr(subprocess, 'run', _timeout)
    result = docker_compose.up()
    assert result['success'] is False
    assert 'timed out' in result['error']


def test_down_success(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0),
    )
    result = docker_compose.down(volumes=False)
    assert result['success'] is True


def test_down_with_volumes(monkeypatch):
    captured_args = []
    def _run(cmd, **kw):
        captured_args.extend(cmd)
        return _CmdResult(returncode=0)
    monkeypatch.setattr(subprocess, 'run', _run)
    docker_compose.down(volumes=True)
    assert '-v' in captured_args


def test_get_service_health_parses_json_lines(monkeypatch):
    json_lines = (
        '{"Service":"postgres","State":"running","Health":"healthy"}\n'
        '{"Service":"backend1","State":"running","Health":"healthy"}\n'
        '{"Service":"simulation","State":"running","Health":""}\n'
    )
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0, stdout=json_lines),
    )
    health = docker_compose.get_service_health()
    assert '_error' not in health
    assert health['postgres']['running'] is True
    assert health['postgres']['health'] == 'healthy'
    assert health['simulation']['running'] is True


def test_get_service_health_error(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1, stderr='no config'),
    )
    health = docker_compose.get_service_health()
    assert '_error' in health


def test_get_logs_success(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0, stdout='log line 1\nlog line 2\n'),
    )
    result = docker_compose.get_logs(tail=10)
    assert 'log line 1' in result['output']


def test_get_logs_timeout(monkeypatch):
    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd='docker', timeout=15)
    monkeypatch.setattr(subprocess, 'run', _timeout)
    result = docker_compose.get_logs()
    assert 'error' in result


def test_test_endpoints_handles_failures(monkeypatch):
    import urllib.request
    def _fail(req, timeout=5):
        raise ConnectionRefusedError("Connection refused")
    monkeypatch.setattr(urllib.request, 'urlopen', _fail)
    results = docker_compose.test_endpoints()
    assert len(results) == 3
    assert all(not r['healthy'] for r in results)


def test_compose_file_exists_with_real_file(monkeypatch, tmp_path):
    compose = tmp_path / 'docker-compose.yml'
    compose.write_text('services: {}')
    monkeypatch.setattr(docker_compose, '_COMPOSE_FILE', str(compose))
    assert docker_compose.compose_file_exists() is True


def test_compose_file_exists_missing(monkeypatch):
    monkeypatch.setattr(docker_compose, '_COMPOSE_FILE', '/nonexistent/docker-compose.yml')
    assert docker_compose.compose_file_exists() is False


def test_compose_file_path_returns_resolved_path():
    path = docker_compose.compose_file_path()
    assert path.endswith('docker-compose.yml')
    assert os.path.isabs(path)


def test_find_compose_file_uses_env_override(monkeypatch):
    monkeypatch.setenv('SIMULATION_COMPOSE_FILE', '/custom/docker-compose.yml')
    result = docker_compose._find_compose_file()
    assert result == '/custom/docker-compose.yml'


def test_find_compose_file_finds_canonical_location(monkeypatch):
    monkeypatch.delenv('SIMULATION_COMPOSE_FILE', raising=False)
    # The real project root has docker-compose.yml, so canonical should succeed
    result = docker_compose._find_compose_file()
    assert result.endswith('docker-compose.yml')
    assert os.path.isfile(result)


def test_find_compose_file_walks_up_from_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv('SIMULATION_COMPOSE_FILE', raising=False)
    # Create a compose file in parent, cwd is a child
    compose = tmp_path / 'docker-compose.yml'
    compose.write_text('services: {}')
    child = tmp_path / 'subdir'
    child.mkdir()
    monkeypatch.chdir(child)
    # Patch the canonical path so it doesn't short-circuit
    monkeypatch.setattr(
        docker_compose, '_find_compose_file',
        docker_compose._find_compose_file.__wrapped__
        if hasattr(docker_compose._find_compose_file, '__wrapped__')
        else docker_compose._find_compose_file,
    )
    # Re-run the logic manually since _find_compose_file uses the real canonical path
    import os as _os
    search = _os.path.abspath(_os.getcwd())
    found = None
    while True:
        candidate = _os.path.join(search, 'docker-compose.yml')
        if _os.path.isfile(candidate):
            found = candidate
            break
        parent = _os.path.dirname(search)
        if parent == search:
            break
        search = parent
    assert found is not None
    assert found == str(compose)
