from simulation_service_tool.services import cluster_init, hung_api_cleanup


def test_clear_hung_api_listeners_noop_when_none(monkeypatch):
    statuses = {
        '5001': {'service': 'Backend API'},
        '5002': {'service': 'Simulation Service'},
    }

    monkeypatch.setattr(hung_api_cleanup, 'get_port_status', lambda: statuses)
    monkeypatch.setattr(hung_api_cleanup, 'check_hung_dev_services', lambda statuses_arg: [])

    result = hung_api_cleanup.clear_hung_api_listeners()

    assert result['success'] is True
    assert result['detail'] == 'none detected'
    assert result['released_ports'] == []


def test_clear_hung_api_listeners_kills_only_api_ports(monkeypatch):
    statuses = {
        '3000': {'service': 'Frontend'},
        '5001': {'service': 'Backend API'},
        '5002': {'service': 'Simulation Service'},
    }

    monkeypatch.setattr(hung_api_cleanup, 'get_port_status', lambda: statuses)
    monkeypatch.setattr(
        hung_api_cleanup,
        'check_hung_dev_services',
        lambda statuses_arg: [
            {'port': '3000'},
            {'port': '5001'},
            {'port': '5002'},
        ],
    )

    killed = []
    monkeypatch.setattr(
        hung_api_cleanup,
        'kill_port',
        lambda port: (killed.append(port) or {'already_free': False, 'failed_pids': [], 'error': None}),
    )

    result = hung_api_cleanup.clear_hung_api_listeners()

    assert result['success'] is True
    assert killed == ['5001', '5002']
    assert result['released_ports'] == ['5001', '5002']
    assert 'Backend API (5001)' in result['detail']
    assert 'Simulation Service (5002)' in result['detail']


def test_clear_hung_api_listeners_reports_failures(monkeypatch):
    statuses = {
        '5001': {'service': 'Backend API'},
        '5002': {'service': 'Simulation Service'},
    }

    monkeypatch.setattr(hung_api_cleanup, 'get_port_status', lambda: statuses)
    monkeypatch.setattr(
        hung_api_cleanup,
        'check_hung_dev_services',
        lambda statuses_arg: [{'port': '5002'}],
    )
    monkeypatch.setattr(
        hung_api_cleanup,
        'kill_port',
        lambda port: {'already_free': False, 'failed_pids': ['1234'], 'error': None},
    )

    result = hung_api_cleanup.clear_hung_api_listeners()

    assert result['success'] is False
    assert 'Simulation Service (5002)' in result['detail']
    assert '1234' in result['detail']


def test_clear_hung_api_listeners_restarts_sim_service_when_requested(monkeypatch):
    statuses = {
        '5002': {'service': 'Simulation Service'},
    }

    monkeypatch.setattr(hung_api_cleanup, 'get_port_status', lambda: statuses)
    monkeypatch.setattr(
        hung_api_cleanup,
        'check_hung_dev_services',
        lambda statuses_arg: [{'port': '5002'}],
    )
    monkeypatch.setattr(
        hung_api_cleanup,
        'kill_port',
        lambda port: {'already_free': False, 'failed_pids': [], 'error': None},
    )
    monkeypatch.setattr(
        'simulation_service_tool.services.smart_diagnostics._restart_service',
        lambda: (True, 'Service restarted and healthy'),
    )

    result = hung_api_cleanup.clear_hung_api_listeners(restart_simulation=True)

    assert result['success'] is True
    assert result['restart_attempted'] is True
    assert result['restart_success'] is True
    assert 'restarted Simulation Service (5002)' in result['detail']


def test_cluster_init_step_uses_restart_mode(monkeypatch):
    calls = []

    monkeypatch.setattr(
        cluster_init,
        'clear_hung_api_listeners',
        lambda restart_simulation=False: calls.append(restart_simulation) or {'success': True, 'detail': 'ok'},
    )

    result = cluster_init._step_clear_hung_api_listeners()

    assert calls == [True]
    assert result == {'success': True, 'detail': 'ok'}


def test_init_steps_start_with_hung_api_cleanup():
    assert cluster_init.INIT_STEPS[0][0] == 'Clearing hung API listeners'