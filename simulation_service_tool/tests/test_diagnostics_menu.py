from simulation_service_tool.menus import diagnostics


def test_quick_diagnostics_returns_expected_keys(monkeypatch):
    monkeypatch.setattr(diagnostics, 'check_service', lambda: False)
    monkeypatch.setattr(diagnostics, 'run_drift_checks', lambda service_running=None: [])
    monkeypatch.setattr(diagnostics, 'diagnose', lambda: {'status': 'healthy', 'running_cluster': 'kind-kind'})
    monkeypatch.setattr(diagnostics, 'compose_file_exists', lambda: False)
    monkeypatch.setattr(diagnostics, 'is_compose_running', lambda: False)
    monkeypatch.setattr(diagnostics, 'test_endpoints', lambda: [])

    result = diagnostics.quick_diagnostics()

    assert 'service_running' in result
    assert 'drift_findings' in result
    assert 'k8s_diag' in result
    assert 'compose_running' in result
    assert 'endpoints' in result
    assert result['service_running'] is False


def test_quick_diagnostics_passes_service_status_to_drift(monkeypatch):
    received = []
    monkeypatch.setattr(diagnostics, 'check_service', lambda: True)
    monkeypatch.setattr(diagnostics, 'run_drift_checks', lambda sr=None: (received.append(sr) or []))
    monkeypatch.setattr(diagnostics, 'diagnose', lambda: {'status': 'healthy'})
    monkeypatch.setattr(diagnostics, 'compose_file_exists', lambda: False)
    monkeypatch.setattr(diagnostics, 'is_compose_running', lambda: False)
    monkeypatch.setattr(diagnostics, 'test_endpoints', lambda: [])

    diagnostics.quick_diagnostics()

    assert received == [True]


def test_quick_diagnostics_includes_compose_health(monkeypatch):
    monkeypatch.setattr(diagnostics, 'check_service', lambda: True)
    monkeypatch.setattr(diagnostics, 'run_drift_checks', lambda sr=None: [])
    monkeypatch.setattr(diagnostics, 'diagnose', lambda: {'status': 'healthy'})
    monkeypatch.setattr(diagnostics, 'compose_file_exists', lambda: True)
    monkeypatch.setattr(diagnostics, 'is_compose_running', lambda: True)
    monkeypatch.setattr(diagnostics, 'get_service_health', lambda: {
        'backend1': {'running': True, 'state': 'running', 'health': ''},
        'postgres': {'running': True, 'state': 'running', 'health': ''},
    })
    monkeypatch.setattr(diagnostics, 'test_endpoints', lambda: [
        {'name': 'backend', 'url': 'http://localhost:5001/health', 'status': 200, 'healthy': True},
    ])

    result = diagnostics.quick_diagnostics()

    assert result['compose_running'] is True
    assert 'backend1' in result['compose_health']
    assert len(result['endpoints']) == 1


def test_render_shows_docker_and_backend_status(monkeypatch, capsys):
    monkeypatch.setattr(diagnostics, 'clear_screen', lambda: None)
    monkeypatch.setattr(diagnostics, 'compose_file_exists', lambda: True)
    diag = {
        'service_running': True,
        'k8s_diag': {'status': 'healthy'},
        'drift_findings': [],
        'compose_running': True,
        'compose_health': {
            'backend1': {'running': True},
            'postgres': {'running': True},
        },
        'endpoints': [
            {'name': 'backend', 'healthy': True},
            {'name': 'simulation', 'healthy': False},
        ],
    }
    diagnostics.render_simple_diagnostics(diag)

    output = capsys.readouterr().out
    assert 'Docker' in output
    assert 'Backend API' in output
    assert '1/2' in output  # 1 of 2 endpoints healthy


def test_render_simple_diagnostics_returns_issues(monkeypatch):
    monkeypatch.setattr(diagnostics, 'clear_screen', lambda: None)
    diag = {
        'service_running': True,
        'k8s_diag': {'status': 'healthy'},
        'drift_findings': [
            {'severity': 'warning', 'check': 'orphaned', 'summary': 'Orphaned release'},
            {'severity': 'info', 'check': 'service_offline', 'summary': 'Service offline'},
        ],
    }
    issues = diagnostics.render_simple_diagnostics(diag)

    assert len(issues) == 1
    assert issues[0]['check'] == 'orphaned'


def test_render_simple_diagnostics_no_issues(monkeypatch):
    monkeypatch.setattr(diagnostics, 'clear_screen', lambda: None)
    diag = {
        'service_running': True,
        'k8s_diag': {'status': 'healthy'},
        'drift_findings': [],
    }
    issues = diagnostics.render_simple_diagnostics(diag)
    assert issues == []


def test_auto_fix_remediates_drift(monkeypatch, capsys):
    monkeypatch.setattr(diagnostics, 'remediate_all', lambda findings, **kw: (True, [
        ('orphaned', True, 'Orphaned resources cleaned'),
    ]))

    issues = [{'severity': 'warning', 'check': 'orphaned', 'summary': 'Orphan', 'action': 'clean_orphans'}]
    diag = {
        'service_running': True,
        'k8s_diag': {'status': 'healthy'},
        'drift_findings': issues,
    }
    result = diagnostics._auto_fix(issues, diag)

    assert result is True
    output = capsys.readouterr().out
    assert 'Orphaned resources cleaned' in output


def test_auto_fix_recovers_k8s(monkeypatch, capsys):
    monkeypatch.setattr(diagnostics, 'diagnose_and_recover', lambda: True)

    diag = {
        'service_running': True,
        'k8s_diag': {'status': 'unreachable'},
        'drift_findings': [],
    }
    result = diagnostics._auto_fix([], diag)

    assert result is True
    output = capsys.readouterr().out
    assert 'K8s is now reachable' in output


def test_auto_fix_starts_service_when_confirmed(monkeypatch):
    started = []
    monkeypatch.setattr(diagnostics, 'start_service', lambda: started.append(True))

    class _Confirm:
        def ask(self):
            return True

    monkeypatch.setattr(diagnostics.questionary, 'confirm', lambda *a, **kw: _Confirm())

    diag = {
        'service_running': False,
        'k8s_diag': {'status': 'healthy'},
        'drift_findings': [],
    }
    diagnostics._auto_fix([], diag)

    assert started == [True]
