"""Tests for kubectl pod watch retry behavior."""

import types

from simulation_service_tool.cli import watch


def _result(returncode=0, stderr=''):
    return types.SimpleNamespace(returncode=returncode, stderr=stderr)


def test_watch_release_pods_retries_transient_api_failures(monkeypatch, capsys):
    calls = []
    sleep_calls = []
    prompt_calls = []
    results = [
        _result(1, 'Unable to connect to the server: net/http: TLS handshake timeout'),
        _result(0, ''),
    ]

    def fake_run_watch_command(release_name):
        calls.append(release_name)
        return results.pop(0)

    monkeypatch.setattr(watch, '_run_watch_command', fake_run_watch_command)
    monkeypatch.setattr(watch.time, 'sleep', lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(watch, '_prompt_go_back', lambda *args, **kwargs: prompt_calls.append((args, kwargs)))

    watch.watch_release_pods_kubectl('ccccc', max_retries=3, retry_delay=5)

    output = capsys.readouterr().out
    assert len(calls) == 2
    assert sleep_calls == [5]
    assert 'TLS handshake timeout' in output
    assert 'The Helm release was already installed' in output
    assert 'Retrying in 5s (attempt 2/3).' in output
    assert prompt_calls


def test_watch_release_pods_prints_manual_guidance_after_retry_exhaustion(monkeypatch, capsys):
    calls = []
    sleep_calls = []
    prompt_calls = []

    def fake_run_watch_command(release_name):
        calls.append(release_name)
        return _result(1, 'Unable to connect to the server: net/http: TLS handshake timeout')

    monkeypatch.setattr(watch, '_run_watch_command', fake_run_watch_command)
    monkeypatch.setattr(watch.time, 'sleep', lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(watch, '_prompt_go_back', lambda *args, **kwargs: prompt_calls.append((args, kwargs)))

    watch.watch_release_pods_kubectl('ccccc', max_retries=3, retry_delay=5)

    output = capsys.readouterr().out
    assert len(calls) == 3
    assert sleep_calls == [5, 5]
    assert 'Could not establish a stable watch connection to the Kubernetes API.' in output
    assert 'The test may still be running. Check status manually with:' in output
    assert 'kubectl get pods -l release=ccccc' in output
    assert prompt_calls


def test_watch_release_pods_handles_keyboard_interrupt(monkeypatch, capsys):
    prompt_calls = []

    def fake_run_watch_command(_release_name):
        raise KeyboardInterrupt

    monkeypatch.setattr(watch, '_run_watch_command', fake_run_watch_command)
    monkeypatch.setattr(watch, '_prompt_go_back', lambda *args, **kwargs: prompt_calls.append((args, kwargs)))

    watch.watch_release_pods_kubectl('ccccc')

    output = capsys.readouterr().out
    assert 'Stopped watching pods.' in output
    assert prompt_calls