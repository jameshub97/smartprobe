from simulation_service_tool.cli import preflight as preflight_mod
from simulation_service_tool.cli import snapshots


def test_get_welcome_snapshot_defers_cluster_scans(monkeypatch):
    monkeypatch.setattr(snapshots, "get_port_status", lambda: {
        "3000": {"in_use": False},
        "5002": {"in_use": True},
    })

    def fail_if_called(*args, **kwargs):
        raise AssertionError("cluster scan should be deferred")

    monkeypatch.setattr(snapshots, "_collect_release_pod_assessment", fail_if_called)
    monkeypatch.setattr(snapshots, "_get_statefulset_stale_status", fail_if_called)
    monkeypatch.setattr(snapshots, "direct_preflight_check", fail_if_called)

    snapshot = snapshots.get_welcome_snapshot(service_running=False)

    assert snapshot["active_ports"] == 1
    assert snapshot["active_pods"] == 0
    assert snapshot["healthy_pods"] == 0
    assert snapshot["pods_pending"] is True
    assert snapshot["preflight_pending"] is True
    assert snapshot["stale_pending"] is True
    assert snapshot["orphaned_count"] == 0
    assert snapshot["stale_pod"] is None


def test_get_welcome_snapshot_includes_requested_cluster_scans(monkeypatch):
    monkeypatch.setattr(snapshots, "get_port_status", lambda: {
        "3000": {"in_use": True},
        "5002": {"in_use": False},
    })
    monkeypatch.setattr(snapshots, "_collect_release_pod_assessment", lambda: {
        "total": 3,
        "healthy": 2,
        "waiting_reasons": ["CrashLoopBackOff"],
        "error": None,
    })
    monkeypatch.setattr(snapshots, "_get_statefulset_stale_status", lambda: {
        "is_stale": True,
        "pod_name": "playwright-agent-0",
    })
    monkeypatch.setattr(snapshots, "direct_preflight_check", lambda: {
        "conflicts": [{"type": "pdb", "name": "playwright-agent-pdb"}],
    })

    snapshot = snapshots.get_welcome_snapshot(
        service_running=True,
        include_preflight=True,
        include_stale=True,
        include_pods=True,
    )

    assert snapshot["active_ports"] == 1
    assert snapshot["active_pods"] == 3
    assert snapshot["healthy_pods"] == 2
    assert snapshot["unhealthy_pods"] == 1
    assert snapshot["waiting_reasons"] == ["CrashLoopBackOff"]
    assert snapshot["pods_pending"] is False
    assert snapshot["preflight_pending"] is False
    assert snapshot["stale_pending"] is False
    assert snapshot["orphaned_count"] == 1
    assert snapshot["stale_pod"]["is_stale"] is True


def test_auto_fix_conflicts_cleans_releases(monkeypatch):
    cleaned = []
    monkeypatch.setattr(preflight_mod, 'direct_release_cleanup', lambda r, dry_run: cleaned.append(r))
    monkeypatch.setattr(preflight_mod, 'run_cli_command', lambda *a, **kw: None)

    preflight = {
        'has_conflicts': True,
        'conflicts': [{'type': 'helm_releases', 'releases': ['test-abc', 'test-xyz']}],
    }
    result = preflight_mod._auto_fix_conflicts(preflight)
    assert result is True
    assert cleaned == ['test-abc', 'test-xyz']


def test_auto_fix_conflicts_cleans_pvcs_and_pdbs(monkeypatch):
    kubectl_calls = []

    class FakeResult:
        returncode = 0
        stdout = ''
        stderr = ''

    def fake_run(args, **kwargs):
        kubectl_calls.append(args)
        return FakeResult()

    monkeypatch.setattr(preflight_mod, 'run_cli_command', fake_run)
    monkeypatch.setattr(preflight_mod, 'direct_release_cleanup', lambda r, dry_run: None)

    preflight = {
        'has_conflicts': True,
        'conflicts': [
            {'type': 'pvc', 'name': 'playwright-cache'},
            {'type': 'pdb', 'name': 'playwright-agent-pdb'},
            {'type': 'helm_releases', 'releases': ['stale-release']},
        ],
    }
    result = preflight_mod._auto_fix_conflicts(preflight)
    assert result is True
    # Should have kubectl delete calls for PVC and PDB
    pvc_calls = [c for c in kubectl_calls if 'pvc' in c]
    pdb_calls = [c for c in kubectl_calls if 'pdb' in c]
    assert len(pvc_calls) >= 1
    assert len(pdb_calls) >= 1


def test_auto_fix_conflicts_empty_returns_false():
    assert preflight_mod._auto_fix_conflicts({'conflicts': []}) is False