"""Tests for smart diagnostics drift detection and remediation."""

import pytest

from simulation_service_tool.services import smart_diagnostics


@pytest.fixture(autouse=True)
def _mock_runtime_gates(monkeypatch):
    """Ensure _docker_running and k8s_reachable always return healthy defaults.

    The real checks would hit Docker / kubectl which may not be available
    in CI.  Individual tests can override these via their own monkeypatch.
    """
    monkeypatch.setattr(smart_diagnostics, '_docker_running', lambda: True)
    # k8s_reachable is imported lazily inside run_drift_checks; patch the
    # module-level function in k8s_connectivity so the deferred import picks
    # it up.
    from simulation_service_tool.services import k8s_connectivity
    monkeypatch.setattr(k8s_connectivity, 'k8s_reachable', lambda *a, **kw: 'reachable')
    # The registry / mirror / node-image probes do real network/docker calls;
    # default them to healthy so tests don't require a running registry or cluster.
    monkeypatch.setattr(smart_diagnostics, '_local_registry_reachable', lambda: True)
    monkeypatch.setattr(smart_diagnostics, '_registry_mirror_fix_applied', lambda node: True)
    monkeypatch.setattr(smart_diagnostics, '_node_has_agent_image', lambda node: True)


def _stub_verify_state(overrides=None):
    base = {
        'helm_test_releases': 0,
        'playwright_pods': 0,
        'playwright_pvcs': 0,
        'conflicting_pdbs': 0,
        'is_clean': True,
    }
    base.update(overrides or {})
    return base


class _CmdResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_drift_checks_clean_cluster_returns_no_warnings(monkeypatch):
    monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state())
    monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])
    monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())

    findings = smart_diagnostics.run_drift_checks(service_running=True)

    warnings = [f for f in findings if f['severity'] in ('warning', 'error')]
    assert warnings == []


def test_run_drift_checks_offline_service_returns_info(monkeypatch):
    monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state())
    monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])
    monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())

    findings = smart_diagnostics.run_drift_checks(service_running=False)

    assert len(findings) == 1
    assert findings[0]['severity'] == 'info'
    assert findings[0]['check'] == 'service_offline'


def test_run_drift_checks_detects_orphaned_releases(monkeypatch):
    monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state({
        'helm_test_releases': 2,
        'is_clean': False,
    }))
    monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: ['test-abc', 'test-xyz'])
    monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())

    findings = smart_diagnostics.run_drift_checks(service_running=True)

    release_findings = [f for f in findings if f['check'] == 'orphaned_releases']
    assert len(release_findings) == 1
    assert '2 orphaned release(s)' in release_findings[0]['summary']
    assert release_findings[0]['action'] == 'clean_orphans'


def test_run_drift_checks_detects_orphaned_pvcs(monkeypatch):
    monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state({
        'playwright_pvcs': 1,
        'is_clean': False,
    }))
    monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])
    monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())

    findings = smart_diagnostics.run_drift_checks(service_running=True)

    pvc_findings = [f for f in findings if f['check'] == 'orphaned_pvcs']
    assert len(pvc_findings) == 1
    assert pvc_findings[0]['severity'] == 'warning'


def test_run_drift_checks_detects_conflicting_pdbs(monkeypatch):
    monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state({
        'conflicting_pdbs': 1,
        'is_clean': False,
    }))
    monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])
    monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())

    findings = smart_diagnostics.run_drift_checks(service_running=True)

    pdb_findings = [f for f in findings if f['check'] == 'conflicting_pdbs']
    assert len(pdb_findings) == 1


def test_run_drift_checks_detects_residual_pods(monkeypatch):
    monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state())
    monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])

    call_count = [0]

    def fake_run(args, **kwargs):
        call_count[0] += 1
        if '--field-selector=status.phase!=Running' in args:
            return _CmdResult(stdout='pod-old-1 pod-old-2')
        return _CmdResult()

    monkeypatch.setattr(smart_diagnostics, 'run_cli_command', fake_run)

    findings = smart_diagnostics.run_drift_checks(service_running=True)

    pod_findings = [f for f in findings if f['check'] == 'residual_pods']
    assert len(pod_findings) == 1
    assert '2 non-running' in pod_findings[0]['summary']


def test_run_drift_checks_detects_unhealthy_pods(monkeypatch):
    monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state())
    monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])

    def fake_run(args, **kwargs):
        if '--field-selector=status.phase=Running' in args:
            return _CmdResult(stdout='agent-0 true\nagent-1 false\n')
        return _CmdResult()

    monkeypatch.setattr(smart_diagnostics, 'run_cli_command', fake_run)

    findings = smart_diagnostics.run_drift_checks(service_running=True)

    unhealthy = [f for f in findings if f['check'] == 'unhealthy_pods']
    assert len(unhealthy) == 1
    assert unhealthy[0]['severity'] == 'error'


def test_has_drift_returns_false_when_clean(monkeypatch):
    monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state())
    monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])
    monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())

    assert smart_diagnostics.has_drift(service_running=True) is False


def test_has_drift_returns_true_with_orphans(monkeypatch):
    monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state({
        'helm_test_releases': 1,
        'is_clean': False,
    }))
    monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: ['stale-test'])
    monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())

    assert smart_diagnostics.has_drift(service_running=True) is True


def test_get_drift_banner_none_when_clean():
    findings = [{'severity': 'info', 'check': 'test', 'summary': 'x', 'remediation': 'y'}]
    assert smart_diagnostics.get_drift_banner(findings) is None


def test_get_drift_banner_single_issue():
    findings = [{'severity': 'warning', 'check': 'test', 'summary': 'One problem', 'remediation': 'Fix it'}]
    banner = smart_diagnostics.get_drift_banner(findings)
    assert banner == 'One problem'


def test_get_drift_banner_multiple_issues():
    findings = [
        {'severity': 'warning', 'check': 'a', 'summary': 'Issue A', 'remediation': 'Fix A'},
        {'severity': 'error', 'check': 'b', 'summary': 'Issue B', 'remediation': 'Fix B'},
    ]
    banner = smart_diagnostics.get_drift_banner(findings)
    assert '2 issue(s)' in banner


def test_auto_remediate_clean_orphans(monkeypatch):
    monkeypatch.setattr(smart_diagnostics, 'direct_quick_cleanup', lambda dry_run: {'errors': []})
    finding = {'severity': 'warning', 'check': 'orphaned_releases', 'summary': 'x', 'remediation': 'y', 'action': 'clean_orphans'}

    success, detail = smart_diagnostics.auto_remediate(finding)
    assert success is True
    assert 'cleaned' in detail.lower()


def test_auto_remediate_service_offline_restarts(monkeypatch):
    health_calls = []

    def fake_check():
        health_calls.append(1)
        return len(health_calls) > 1  # Fail first, succeed on retry

    class FakeResult:
        returncode = 0
        stderr = ''

    monkeypatch.setattr('simulation_service_tool.services.api_client.check_service', fake_check)
    monkeypatch.setattr(smart_diagnostics.subprocess, 'run', lambda *a, **kw: FakeResult())
    monkeypatch.setattr(smart_diagnostics.time, 'sleep', lambda _: None)

    finding = {'severity': 'info', 'check': 'service_offline', 'summary': 'x', 'remediation': 'y', 'action': 'start_service'}

    success, detail = smart_diagnostics.auto_remediate(finding)
    assert success is True
    assert 'healthy' in detail.lower()


def test_restart_service_skips_when_already_healthy(monkeypatch):
    monkeypatch.setattr('simulation_service_tool.services.api_client.check_service', lambda: True)

    success, detail = smart_diagnostics._restart_service()
    assert success is True
    assert 'already healthy' in detail.lower()


def test_restart_service_calls_docker_compose_restart(monkeypatch):
    health_calls = []

    def fake_check():
        health_calls.append(1)
        return len(health_calls) > 2  # Fail initial check; succeed on second poll

    run_calls = []

    class FakeResult:
        returncode = 0
        stderr = ''

    def fake_run(args, **kw):
        run_calls.append(args)
        return FakeResult()

    monkeypatch.setattr('simulation_service_tool.services.api_client.check_service', fake_check)
    monkeypatch.setattr(smart_diagnostics.subprocess, 'run', fake_run)
    monkeypatch.setattr(smart_diagnostics.time, 'sleep', lambda _: None)

    success, detail = smart_diagnostics._restart_service()
    assert success is True
    assert any('docker' in str(a) for a in run_calls)


def test_restart_service_fails_when_docker_compose_fails(monkeypatch):
    monkeypatch.setattr('simulation_service_tool.services.api_client.check_service', lambda: False)

    class FakeResult:
        returncode = 1
        stderr = 'container not found'

    monkeypatch.setattr(smart_diagnostics.subprocess, 'run', lambda *a, **kw: FakeResult())

    success, detail = smart_diagnostics._restart_service()
    assert success is False
    assert 'container not found' in detail


def test_remediate_all_deduplicates_actions(monkeypatch):
    cleanup_calls = []
    monkeypatch.setattr(smart_diagnostics, 'direct_quick_cleanup', lambda dry_run: (cleanup_calls.append(1) or {'errors': []}))

    findings = [
        {'severity': 'warning', 'check': 'orphaned_releases', 'summary': 'x', 'remediation': 'y', 'action': 'clean_orphans'},
        {'severity': 'warning', 'check': 'orphaned_pvcs', 'summary': 'x', 'remediation': 'y', 'action': 'clean_orphans'},
    ]

    all_fixed, results = smart_diagnostics.remediate_all(findings)
    assert all_fixed is True
    assert len(cleanup_calls) == 1  # Only one cleanup call despite two findings
    assert len(results) == 1


def test_remediate_all_runs_service_restart_last(monkeypatch):
    action_order = []
    monkeypatch.setattr(smart_diagnostics, 'direct_quick_cleanup', lambda dry_run: (action_order.append('cleanup') or {'errors': []}))

    health_calls = []

    def fake_check():
        health_calls.append(1)
        return len(health_calls) > 1

    monkeypatch.setattr('simulation_service_tool.services.api_client.check_service', fake_check)
    class FakeResult:
        returncode = 0
        stderr = ''

    def fake_run(args, **kw):
        action_order.append('start')
        return FakeResult()

    monkeypatch.setattr(smart_diagnostics.subprocess, 'run', fake_run)
    monkeypatch.setattr(smart_diagnostics.time, 'sleep', lambda _: None)

    findings = [
        {'severity': 'info', 'check': 'service_offline', 'summary': 'x', 'remediation': 'y', 'action': 'start_service'},
        {'severity': 'warning', 'check': 'orphaned_releases', 'summary': 'x', 'remediation': 'y', 'action': 'clean_orphans'},
    ]

    all_fixed, results = smart_diagnostics.remediate_all(findings)
    assert all_fixed is True
    assert len(results) == 2
    assert action_order == ['cleanup', 'start']  # Cleanup before service start


def test_remediate_all_handles_service_action(monkeypatch):
    monkeypatch.setattr('simulation_service_tool.services.api_client.check_service', lambda: True)

    findings = [
        {'severity': 'info', 'check': 'service_offline', 'summary': 'x', 'remediation': 'y', 'action': 'start_service'},
    ]

    all_fixed, results = smart_diagnostics.remediate_all(findings)
    assert all_fixed is True
    assert len(results) == 1
    assert 'already healthy' in results[0][2].lower()


def test_restart_service_starts_when_port_free(monkeypatch):
    """docker compose restart succeeds; health check passes on first poll."""
    health_calls = []

    def fake_check():
        health_calls.append(1)
        return len(health_calls) > 1  # Fail initial check, pass on first retry

    class FakeResult:
        returncode = 0
        stderr = ''

    run_calls = []
    monkeypatch.setattr('simulation_service_tool.services.api_client.check_service', fake_check)
    monkeypatch.setattr(smart_diagnostics.subprocess, 'run', lambda *a, **kw: (run_calls.append(a[0]) or FakeResult()))
    monkeypatch.setattr(smart_diagnostics.time, 'sleep', lambda _: None)

    success, detail = smart_diagnostics._restart_service()

    assert success is True
    assert 'healthy' in detail.lower()
    assert len(run_calls) == 1


def test_restart_service_times_out_when_never_healthy(monkeypatch):
    """docker compose restart succeeds but container never passes health check."""
    monkeypatch.setattr('simulation_service_tool.services.api_client.check_service', lambda: False)

    class FakeResult:
        returncode = 0
        stderr = ''

    monkeypatch.setattr(smart_diagnostics.subprocess, 'run', lambda *a, **kw: FakeResult())
    monkeypatch.setattr(smart_diagnostics.time, 'sleep', lambda _: None)

    success, detail = smart_diagnostics._restart_service()

    assert success is False
    assert 'not responding' in detail.lower()


def test_restart_service_returns_false_on_keyboard_interrupt(monkeypatch):
    """KeyboardInterrupt during the health-poll loop is caught and reported."""
    call_count = [0]

    def fake_check():
        call_count[0] += 1
        if call_count[0] > 1:
            raise KeyboardInterrupt
        return False

    class FakeResult:
        returncode = 0
        stderr = ''

    monkeypatch.setattr('simulation_service_tool.services.api_client.check_service', fake_check)
    monkeypatch.setattr(smart_diagnostics.subprocess, 'run', lambda *a, **kw: FakeResult())
    monkeypatch.setattr(smart_diagnostics.time, 'sleep', lambda _: None)

    success, detail = smart_diagnostics._restart_service()

    assert success is False
    assert 'cancel' in detail.lower()


def test_run_drift_checks_detects_missing_compose_file(monkeypatch):
    """When docker-compose.yml is missing, a warning finding should appear."""
    monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state())
    monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])
    monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())

    from simulation_service_tool.services import docker_compose
    monkeypatch.setattr(docker_compose, '_COMPOSE_FILE', '/nonexistent/docker-compose.yml')

    findings = smart_diagnostics.run_drift_checks(service_running=True)

    compose_findings = [f for f in findings if f['check'] == 'compose_file_missing']
    assert len(compose_findings) == 1
    assert compose_findings[0]['severity'] == 'warning'
    assert 'not found' in compose_findings[0]['summary']


def test_run_drift_checks_no_compose_warning_when_file_exists(monkeypatch):
    """Compose file present should not trigger compose_file_missing."""
    monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state())
    monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])
    monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())

    findings = smart_diagnostics.run_drift_checks(service_running=True)

    compose_findings = [f for f in findings if f['check'] == 'compose_file_missing']
    assert compose_findings == []


# ── New feedback loop: local registry down ───────────────────────────────────

class TestLocalRegistryDriftCheck:
    def _base_patch(self, monkeypatch):
        monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state())
        monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])
        monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())
        # Default: mirror ok, node has image — isolate the registry check
        monkeypatch.setattr(smart_diagnostics, '_registry_mirror_fix_applied', lambda n: True)
        monkeypatch.setattr(smart_diagnostics, '_node_has_agent_image', lambda n: True)
        monkeypatch.setattr(smart_diagnostics, '_first_kind_worker', lambda: 'desktop-worker')

    def test_registry_down_surfaces_error_finding(self, monkeypatch):
        self._base_patch(monkeypatch)
        monkeypatch.setattr(smart_diagnostics, '_local_registry_reachable', lambda: False)

        findings = smart_diagnostics.run_drift_checks(service_running=True)

        reg_findings = [f for f in findings if f['check'] == 'local_registry_down']
        assert len(reg_findings) == 1
        assert reg_findings[0]['severity'] == 'error'
        assert reg_findings[0]['action'] == 'start_local_registry'

    def test_registry_up_no_finding(self, monkeypatch):
        self._base_patch(monkeypatch)
        monkeypatch.setattr(smart_diagnostics, '_local_registry_reachable', lambda: True)

        findings = smart_diagnostics.run_drift_checks(service_running=True)

        assert not any(f['check'] == 'local_registry_down' for f in findings)

    def test_auto_remediate_starts_existing_container(self, monkeypatch):
        runs = []

        def fake_run(args, **kwargs):
            runs.append(args)

            class R:
                returncode = 0
                stdout = ''
                stderr = ''
            return R()

        monkeypatch.setattr(smart_diagnostics.subprocess, 'run', fake_run)

        finding = {
            'check': 'local_registry_down',
            'action': 'start_local_registry',
            'severity': 'error',
            'summary': '',
            'remediation': '',
        }
        success, detail = smart_diagnostics.auto_remediate(finding)
        assert success is True
        assert 'started' in detail.lower() or 'created' in detail.lower()
        assert any('docker' in a[0] for a in runs)


# ── New feedback loop: registry mirror HTTP mismatch ─────────────────────────

class TestRegistryMirrorDriftCheck:
    def _base_patch(self, monkeypatch):
        monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state())
        monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])
        monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())
        # Default: registry up, node has image — isolate the mirror check
        monkeypatch.setattr(smart_diagnostics, '_local_registry_reachable', lambda: True)
        monkeypatch.setattr(smart_diagnostics, '_node_has_agent_image', lambda n: True)
        monkeypatch.setattr(smart_diagnostics, '_first_kind_worker', lambda: 'desktop-worker')

    def test_mirror_not_fixed_surfaces_error_finding(self, monkeypatch):
        self._base_patch(monkeypatch)
        monkeypatch.setattr(smart_diagnostics, '_registry_mirror_fix_applied', lambda n: False)

        findings = smart_diagnostics.run_drift_checks(service_running=True)

        mirror_findings = [f for f in findings if f['check'] == 'registry_mirror_misconfig']
        assert len(mirror_findings) == 1
        assert mirror_findings[0]['severity'] == 'error'
        assert mirror_findings[0]['action'] == 'fix_registry_mirror'

    def test_mirror_fixed_no_finding(self, monkeypatch):
        self._base_patch(monkeypatch)
        monkeypatch.setattr(smart_diagnostics, '_registry_mirror_fix_applied', lambda n: True)

        findings = smart_diagnostics.run_drift_checks(service_running=True)

        assert not any(f['check'] == 'registry_mirror_misconfig' for f in findings)

    def test_mirror_none_when_no_nodes_no_finding(self, monkeypatch):
        """When no worker nodes are visible, mirror check returns None — should not surface."""
        self._base_patch(monkeypatch)
        monkeypatch.setattr(smart_diagnostics, '_first_kind_worker', lambda: None)

        findings = smart_diagnostics.run_drift_checks(service_running=True)

        assert not any(f['check'] == 'registry_mirror_misconfig' for f in findings)

    def test_auto_remediate_calls_patch_all_nodes(self, monkeypatch):
        patch_calls = []
        monkeypatch.setattr(
            'simulation_service_tool.menus.image_pull._run_patch_all_nodes',
            lambda: (patch_calls.append(1) or (3, 3)),
        )

        finding = {
            'check': 'registry_mirror_misconfig',
            'action': 'fix_registry_mirror',
            'severity': 'error',
            'summary': '',
            'remediation': '',
        }
        success, detail = smart_diagnostics.auto_remediate(finding)
        assert success is True
        assert '3' in detail
        assert patch_calls == [1]


# ── New feedback loop: node image missing ────────────────────────────────────

class TestNodeImageMissingDriftCheck:
    def _base_patch(self, monkeypatch):
        monkeypatch.setattr(smart_diagnostics, 'direct_verify_state', lambda: _stub_verify_state())
        monkeypatch.setattr(smart_diagnostics, 'get_test_releases', lambda: [])
        monkeypatch.setattr(smart_diagnostics, 'run_cli_command', lambda *a, **kw: _CmdResult())
        # Default: registry up, mirror ok — isolate the node image check
        monkeypatch.setattr(smart_diagnostics, '_local_registry_reachable', lambda: True)
        monkeypatch.setattr(smart_diagnostics, '_registry_mirror_fix_applied', lambda n: True)
        monkeypatch.setattr(smart_diagnostics, '_first_kind_worker', lambda: 'desktop-worker')

    def test_image_missing_surfaces_warning_finding(self, monkeypatch):
        self._base_patch(monkeypatch)
        monkeypatch.setattr(smart_diagnostics, '_node_has_agent_image', lambda n: False)

        findings = smart_diagnostics.run_drift_checks(service_running=True)

        img_findings = [f for f in findings if f['check'] == 'node_image_missing']
        assert len(img_findings) == 1
        assert img_findings[0]['severity'] == 'warning'
        assert img_findings[0]['action'] == 'kind_load_image'

    def test_image_present_no_finding(self, monkeypatch):
        self._base_patch(monkeypatch)
        monkeypatch.setattr(smart_diagnostics, '_node_has_agent_image', lambda n: True)

        findings = smart_diagnostics.run_drift_checks(service_running=True)

        assert not any(f['check'] == 'node_image_missing' for f in findings)

    def test_image_none_when_no_nodes_no_finding(self, monkeypatch):
        self._base_patch(monkeypatch)
        monkeypatch.setattr(smart_diagnostics, '_first_kind_worker', lambda: None)

        findings = smart_diagnostics.run_drift_checks(service_running=True)

        assert not any(f['check'] == 'node_image_missing' for f in findings)

    def test_auto_remediate_kind_load(self, monkeypatch):
        runs = []

        def fake_run(args, **kwargs):
            runs.append(args)

            class R:
                returncode = 0
                stdout = 'Image loaded'
                stderr = ''
            return R()

        monkeypatch.setattr(smart_diagnostics.subprocess, 'run', fake_run)

        finding = {
            'check': 'node_image_missing',
            'action': 'kind_load_image',
            'severity': 'warning',
            'summary': '',
            'remediation': '',
        }
        success, detail = smart_diagnostics.auto_remediate(finding)
        assert success is True
        assert 'playwright-agent' in detail
        assert any('kind' in ' '.join(a) for a in runs)
