from simulation_service_tool.menus import cleanup, routine_checks
from simulation_service_tool.ui import display


def test_ensure_cleanup_loaded_builds_issue_remediation(monkeypatch):
    monkeypatch.setattr(cleanup, 'check_service', lambda: False)
    monkeypatch.setattr(cleanup, 'direct_verify_state', lambda: {
        'helm_test_releases': 2,
        'playwright_pods': 1,
        'playwright_pvcs': 1,
        'conflicting_pdbs': 0,
        'is_clean': False,
    })

    cache = cleanup.ensure_cleanup_loaded()

    assert cache['has_issues'] is True
    assert cache['issues'][0]['summary'] == '2 test release(s) still installed'
    assert 'Quick Clean' in cache['issues'][0]['remediation']


def test_build_cleanup_choices_prefers_quick_clean_when_issues_exist():
    choices = cleanup._build_cleanup_choices({'has_issues': True})

    assert choices[0].value == 'quick_clean'
    assert 'recommended' in choices[0].title.lower()


def test_build_cleanup_choices_prefers_verify_when_clean():
    choices = cleanup._build_cleanup_choices({'has_issues': False})

    assert choices[0].value == 'verify'


def test_build_routine_issues_contains_remediation_for_pending_and_stale():
    issues = routine_checks._build_routine_issues({
        'pods_pending': True,
        'preflight_pending': False,
        'preflight_conflicts': [],
        'stale_pending': False,
        'stale_pod': {'is_stale': True, 'pod_name': 'playwright-agent-0'},
        'unhealthy_pods': [],
    })

    assert issues[0]['summary'] == 'pod status has not been loaded yet'
    assert 'Refresh' in issues[0]['remediation']
    assert any('stale pod detected' in issue['summary'] for issue in issues)


def test_build_welcome_issues_contains_service_and_scan_remediation():
    issues = display.build_welcome_issues(False, {
        'pods_pending': True,
        'preflight_pending': True,
        'stale_pending': True,
        'unhealthy_pods': 0,
        'orphaned_count': 0,
        'stale_pod': None,
    })

    assert issues[0]['summary'] == 'simulation service is offline'
    assert 'Start Service' in issues[0]['remediation']
    assert any('full Kubernetes scan has not been loaded yet' == issue['summary'] for issue in issues)
