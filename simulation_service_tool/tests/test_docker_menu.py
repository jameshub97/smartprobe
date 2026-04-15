"""Tests for Docker Compose menu state loading."""

from simulation_service_tool.menus import docker as docker_menu_mod


def test_ensure_docker_loaded_no_docker(monkeypatch):
    monkeypatch.setattr(docker_menu_mod, 'is_docker_available', lambda: False)
    monkeypatch.setattr(docker_menu_mod, 'compose_file_exists', lambda: True)
    monkeypatch.setattr(docker_menu_mod, 'is_compose_running', lambda: False)

    state = docker_menu_mod.ensure_docker_loaded()

    assert state['docker_available'] is False
    assert state['running'] is False
    assert len(state['issues']) == 1
    assert 'not reachable' in state['issues'][0]['summary']


def test_ensure_docker_loaded_not_running(monkeypatch):
    monkeypatch.setattr(docker_menu_mod, 'is_docker_available', lambda: True)
    monkeypatch.setattr(docker_menu_mod, 'compose_file_exists', lambda: True)
    monkeypatch.setattr(docker_menu_mod, 'is_compose_running', lambda: False)

    state = docker_menu_mod.ensure_docker_loaded()

    assert state['docker_available'] is True
    assert state['running'] is False
    assert any('not running' in i['summary'] for i in state['issues'])


def test_ensure_docker_loaded_all_healthy(monkeypatch):
    monkeypatch.setattr(docker_menu_mod, 'is_docker_available', lambda: True)
    monkeypatch.setattr(docker_menu_mod, 'compose_file_exists', lambda: True)
    monkeypatch.setattr(docker_menu_mod, 'is_compose_running', lambda: True)

    healthy_services = {
        svc: {'state': 'running', 'health': 'healthy', 'running': True}
        for svc in docker_menu_mod.EXPECTED_SERVICES
    }
    monkeypatch.setattr(docker_menu_mod, 'get_service_health', lambda: healthy_services)

    state = docker_menu_mod.ensure_docker_loaded()

    assert state['docker_available'] is True
    assert state['running'] is True
    assert state['issues'] == []


def test_ensure_docker_loaded_unhealthy_service(monkeypatch):
    monkeypatch.setattr(docker_menu_mod, 'is_docker_available', lambda: True)
    monkeypatch.setattr(docker_menu_mod, 'compose_file_exists', lambda: True)
    monkeypatch.setattr(docker_menu_mod, 'is_compose_running', lambda: True)

    services = {
        svc: {'state': 'running', 'health': 'healthy', 'running': True}
        for svc in docker_menu_mod.EXPECTED_SERVICES
    }
    services['simulation'] = {'state': 'running', 'health': 'unhealthy', 'running': True}
    monkeypatch.setattr(docker_menu_mod, 'get_service_health', lambda: services)

    state = docker_menu_mod.ensure_docker_loaded()

    assert len(state['issues']) == 1
    assert 'simulation' in state['issues'][0]['summary']
    assert 'unhealthy' in state['issues'][0]['summary']


def test_ensure_docker_loaded_missing_service(monkeypatch):
    monkeypatch.setattr(docker_menu_mod, 'is_docker_available', lambda: True)
    monkeypatch.setattr(docker_menu_mod, 'compose_file_exists', lambda: True)
    monkeypatch.setattr(docker_menu_mod, 'is_compose_running', lambda: True)

    # Only some services running
    services = {
        'postgres': {'state': 'running', 'health': 'healthy', 'running': True},
        'backend1': {'state': 'running', 'health': 'healthy', 'running': True},
    }
    monkeypatch.setattr(docker_menu_mod, 'get_service_health', lambda: services)

    state = docker_menu_mod.ensure_docker_loaded()

    # Should report missing services
    missing_issues = [i for i in state['issues'] if 'not running' in i['summary']]
    assert len(missing_issues) > 0


def test_ensure_docker_loaded_no_compose_file(monkeypatch):
    monkeypatch.setattr(docker_menu_mod, 'is_docker_available', lambda: True)
    monkeypatch.setattr(docker_menu_mod, 'compose_file_exists', lambda: False)
    monkeypatch.setattr(docker_menu_mod, 'is_compose_running', lambda: False)

    state = docker_menu_mod.ensure_docker_loaded()

    assert any('not found' in i['summary'] for i in state['issues'])
