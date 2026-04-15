from simulation_service_tool.cli import preflight as preflight_mod
from simulation_service_tool.menus import cleanup, routine_checks


def test_apply_cached_preflight_result_marks_snapshot_loaded():
    snapshot = {
        'preflight_conflicts': [],
        'preflight_pending': True,
    }
    cached_preflight = {
        'has_conflicts': False,
        'conflicts': [],
    }

    updated = routine_checks._apply_cached_preflight_result(snapshot, cached_preflight)

    assert updated['preflight_pending'] is False
    assert updated['preflight_conflicts'] == []
    assert snapshot['preflight_pending'] is True


def test_apply_cached_preflight_result_ignores_cancelled_result():
    snapshot = {
        'preflight_conflicts': [],
        'preflight_pending': True,
    }

    updated = routine_checks._apply_cached_preflight_result(snapshot, {'cancelled': True})

    assert updated is snapshot
    assert updated['preflight_pending'] is True


def test_preflight_check_returns_result_for_dashboard_cache(monkeypatch):
    monkeypatch.setattr(preflight_mod, 'clear_screen', lambda: None)
    monkeypatch.setattr(preflight_mod, '_prompt_go_back', lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight_mod, '_get_preflight', lambda service_running: {
        'has_conflicts': False,
        'conflicts': [],
    })

    result = preflight_mod.preflight_check(service_running=True)

    assert result == {
        'has_conflicts': False,
        'conflicts': [],
    }


def test_preflight_check_auto_fixes_known_conflicts(monkeypatch):
    monkeypatch.setattr(preflight_mod, 'clear_screen', lambda: None)
    monkeypatch.setattr(preflight_mod, '_prompt_go_back', lambda *args, **kwargs: None)

    preflight_results = iter([
        {
            'has_conflicts': True,
            'conflicts': [
                {'type': 'pvc', 'name': 'playwright-cache', 'fix': 'Delete the stale PVC.'},
            ],
        },
        {
            'has_conflicts': False,
            'conflicts': [],
        },
    ])
    auto_fixed = []

    monkeypatch.setattr(preflight_mod, '_get_preflight', lambda service_running: next(preflight_results))
    monkeypatch.setattr(preflight_mod, '_auto_fix_conflicts', lambda preflight: auto_fixed.append(preflight) or True)

    result = preflight_mod.preflight_check(service_running=True)

    assert len(auto_fixed) == 1
    assert result == {
        'has_conflicts': False,
        'conflicts': [],
    }


def test_handle_remaining_preflight_conflicts_uses_cleanup_center(monkeypatch):
    cleanup_calls = []

    class _Select:
        def ask(self):
            return 'cleanup'

    monkeypatch.setattr(preflight_mod.questionary, 'select', lambda *args, **kwargs: _Select())
    monkeypatch.setattr(cleanup, 'cleanup_menu', lambda: cleanup_calls.append('cleanup'))
    monkeypatch.setattr(preflight_mod, '_get_preflight', lambda service_running: {
        'has_conflicts': False,
        'conflicts': [],
    })

    result = preflight_mod._handle_remaining_preflight_conflicts(
        {
            'has_conflicts': True,
            'conflicts': [
                {'type': 'pdb', 'name': 'playwright-agent-pdb', 'fix': 'Delete the conflicting PDB.'},
            ],
        },
        service_running=True,
        allow_force=False,
    )

    assert result is True
    assert cleanup_calls == ['cleanup']
