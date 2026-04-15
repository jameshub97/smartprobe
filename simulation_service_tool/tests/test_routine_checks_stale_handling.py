from simulation_service_tool.cli import pod_diagnostics
from simulation_service_tool.menus import routine_checks


def test_routine_checks_choices_prefer_stale_inspection():
    choices = routine_checks._build_routine_check_choices({
        'stale_pod': {
            'is_stale': True,
            'pod_name': 'playwright-agent-0',
        }
    })

    assert choices[0].value == 'inspect_stale'
    assert 'stale pod' in choices[0].title.lower()


def test_routine_checks_choices_default_to_diagnosis_when_not_stale():
    choices = routine_checks._build_routine_check_choices({
        'stale_pod': {
            'is_stale': False,
        }
    })

    assert choices[0].value == 'diagnose_unhealthy'
    assert 'diagnose unhealthy pod' in choices[0].title.lower()


def test_diagnose_unhealthy_pod_pauses_on_stale_statefulset(monkeypatch, capsys):
    pod = {
        'metadata': {
            'name': 'playwright-agent-0',
            'creationTimestamp': '2026-04-14T00:00:00Z',
            'ownerReferences': [{'kind': 'StatefulSet', 'name': 'playwright-agent'}],
            'labels': {'release': 'playwright-agent'},
        },
        'status': {
            'phase': 'Running',
            'containerStatuses': [
                {
                    'ready': False,
                    'restartCount': 3,
                    'state': {'waiting': {'reason': 'CrashLoopBackOff'}},
                }
            ],
        },
    }

    monkeypatch.setattr(pod_diagnostics, 'clear_screen', lambda: None)
    monkeypatch.setattr(pod_diagnostics, '_prompt_go_back', lambda *args, **kwargs: None)
    monkeypatch.setattr(pod_diagnostics, '_kubectl_list_json', lambda *args, **kwargs: ([pod], None))
    monkeypatch.setattr(pod_diagnostics, '_get_statefulset_stale_status', lambda **kwargs: {
        'pod_name': 'playwright-agent-0',
        'pod_revision': 'old-rev',
        'current_revision': 'new-rev',
        'pod_created': '2026-04-14T00:00:00Z',
        'waiting_reason': 'CrashLoopBackOff',
        'is_stale': True,
        'is_crashing': True,
    })

    def fail_if_called(*args, **kwargs):
        raise AssertionError('log collection should not run for stale StatefulSet pods')

    monkeypatch.setattr(pod_diagnostics, '_get_pod_logs_output', fail_if_called)

    pod_diagnostics.diagnose_unhealthy_pod(service_running=False)

    output = capsys.readouterr().out
    assert 'Selected pod belongs to a stale StatefulSet revision.' in output
    assert 'Diagnosis is paused because stale revisions can make pod logs misleading.' in output
    assert 'Open Cleanup Center or recreate the StatefulSet pod before running unhealthy pod diagnosis.' in output
