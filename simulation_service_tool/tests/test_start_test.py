from types import SimpleNamespace

from simulation_service_tool.cli import start_test


def test_build_direct_helm_values_includes_probe_url():
    payload = {
        'completions': 100,
        'parallelism': 20,
        'persona': 'browser',
        'mode': 'basic',
        'probeUrl': 'http://localhost:5174/',
    }

    values = start_test._build_direct_helm_values(payload)

    assert values['probeMode'] == 'basic'
    assert values['probeUrl'] == 'http://localhost:5174'


def test_run_direct_helm_install_syncs_active_test_state(monkeypatch):
    payload = {
        'completions': 100,
        'parallelism': 20,
        'persona': 'browser',
        'mode': 'basic',
        'probeUrl': 'http://localhost:5174',
    }
    helm_commands = []
    service_calls = []

    monkeypatch.setattr(start_test, '_resolve_binary', lambda _: 'helm')
    monkeypatch.setattr(start_test, '_locate_helm_chart', lambda: '/tmp/chart')
    monkeypatch.setattr(start_test, 'run_cli_command', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        start_test.subprocess,
        'run',
        lambda cmd, **kwargs: helm_commands.append(cmd) or SimpleNamespace(returncode=0, stdout='', stderr=''),
    )
    monkeypatch.setattr(
        start_test,
        'call_service',
        lambda endpoint, method='GET', data=None: service_calls.append((endpoint, method, data)) or {'status': 'ok'},
    )

    ok, err = start_test._run_direct_helm_install('large-123', payload)

    assert ok is True
    assert err == ''
    assert ['--set', 'probeUrl=http://localhost:5174'] == helm_commands[0][-2:]
    assert service_calls == [
        (
            '/api/simulation/active-test',
            'POST',
            {
                'target_url': 'http://localhost:5174',
                'probe_mode': 'basic',
                'test_name': 'large-123',
                'completions': '100',
                'parallelism': '20',
            },
        )
    ]