from simulation_service_tool.cli import preflight as preflight_mod


def test_get_preflight_runs_hung_cleanup_when_service_offline(monkeypatch):
    cleanup_calls = []

    monkeypatch.setattr(
        preflight_mod,
        'clear_hung_api_listeners',
        lambda restart_simulation=False: cleanup_calls.append(restart_simulation) or {
            'success': True,
            'detail': 'none detected',
            'released_ports': [],
            'failures': [],
            'restart_attempted': False,
            'restart_success': None,
            'restart_detail': None,
        },
    )
    monkeypatch.setattr(preflight_mod, 'direct_preflight_check', lambda: {'mode': 'direct'})

    result = preflight_mod._get_preflight(service_running=False)

    assert cleanup_calls == [False]
    assert result == {'mode': 'direct'}


def test_get_preflight_switches_to_direct_mode_after_clearing_hung_sim_listener(monkeypatch, capsys):
    monkeypatch.setattr(
        preflight_mod,
        'clear_hung_api_listeners',
        lambda restart_simulation=False: {
            'success': True,
            'detail': 'released Simulation Service (5002)',
            'released_ports': ['5002'],
            'failures': [],
            'restart_attempted': False,
            'restart_success': None,
            'restart_detail': None,
        },
    )
    monkeypatch.setattr(preflight_mod, 'direct_preflight_check', lambda: {'mode': 'direct'})

    def _unexpected_probe(*args, **kwargs):
        raise AssertionError('simulation API probe should not run after clearing hung 5002')

    monkeypatch.setattr(preflight_mod, '_probe_sim_api', _unexpected_probe)

    result = preflight_mod._get_preflight(service_running=True)
    output = capsys.readouterr().out

    assert result == {'mode': 'direct'}
    assert 'Cleared hung API listeners before preflight' in output
    assert 'Falling back to direct preflight checks' in output


def test_get_preflight_warns_on_incomplete_cleanup_but_continues(monkeypatch, capsys):
    monkeypatch.setattr(
        preflight_mod,
        'clear_hung_api_listeners',
        lambda restart_simulation=False: {
            'success': False,
            'detail': 'failed Backend API (5001): could not release PID(s) 1234',
            'released_ports': [],
            'failures': ['Backend API (5001): could not release PID(s) 1234'],
            'restart_attempted': False,
            'restart_success': None,
            'restart_detail': None,
        },
    )
    monkeypatch.setattr(preflight_mod, '_probe_sim_api', lambda timeout=2.5: True)
    monkeypatch.setattr(preflight_mod, '_check_docker_services', lambda: {'simulation': True, 'backend': True})
    monkeypatch.setattr(preflight_mod, 'call_service', lambda endpoint: {'has_conflicts': False, 'conflicts': []})

    result = preflight_mod._get_preflight(service_running=True)
    output = capsys.readouterr().out

    assert result == {'has_conflicts': False, 'conflicts': []}
    assert 'Hung API cleanup before preflight was incomplete' in output