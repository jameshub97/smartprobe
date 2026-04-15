"""Tests for K8s connectivity service module."""

import subprocess

from simulation_service_tool.services import k8s_connectivity


class _CmdResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.args = []


# ---------------------------------------------------------------------------
# k8s_reachable()
# ---------------------------------------------------------------------------

def test_k8s_reachable_when_docker_down(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, '_docker_running', lambda: False)
    assert k8s_connectivity.k8s_reachable() == "unreachable"


def test_k8s_reachable_when_cluster_info_ok(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, '_docker_running', lambda: True)
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0, stdout='Kubernetes control plane'),
    )
    assert k8s_connectivity.k8s_reachable() == "reachable"


def test_k8s_reachable_connection_refused(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, '_docker_running', lambda: True)
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1, stderr='dial tcp: connection refused to localhost:6443'),
    )
    assert k8s_connectivity.k8s_reachable() == "unreachable"


def test_k8s_reachable_tls_handshake(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, '_docker_running', lambda: True)
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1, stderr='net/http: TLS handshake timeout'),
    )
    assert k8s_connectivity.k8s_reachable() == "unreachable"


def test_k8s_reachable_timeout_expired(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, '_docker_running', lambda: True)
    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd='kubectl', timeout=3)
    monkeypatch.setattr(subprocess, 'run', _timeout)
    assert k8s_connectivity.k8s_reachable() == "unreachable"


def test_k8s_reachable_kubectl_not_found(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, '_docker_running', lambda: True)
    def _not_found(*a, **kw):
        raise FileNotFoundError("kubectl not found")
    monkeypatch.setattr(subprocess, 'run', _not_found)
    assert k8s_connectivity.k8s_reachable() == "kubectl not found"


def test_k8s_reachable_unknown_error(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, '_docker_running', lambda: True)
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1, stderr='Something unexpected went wrong here'),
    )
    result = k8s_connectivity.k8s_reachable()
    assert result.startswith("error:")


# ---------------------------------------------------------------------------
# context_reachable()
# ---------------------------------------------------------------------------

def test_context_reachable_true(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0),
    )
    assert k8s_connectivity.context_reachable("minikube") is True


def test_context_reachable_false(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1),
    )
    assert k8s_connectivity.context_reachable("minikube") is False


def test_context_reachable_on_exception(monkeypatch):
    def _boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd='kubectl', timeout=2)
    monkeypatch.setattr(subprocess, 'run', _boom)
    assert k8s_connectivity.context_reachable("minikube") is False


# ---------------------------------------------------------------------------
# get_available_contexts()
# ---------------------------------------------------------------------------

def test_get_available_contexts_success(monkeypatch):
    call_count = [0]
    def _run(*a, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            return _CmdResult(returncode=0, stdout='docker-desktop')
        return _CmdResult(returncode=0, stdout='docker-desktop\nminikube\nkind-kind\n')
    monkeypatch.setattr(subprocess, 'run', _run)
    current, contexts = k8s_connectivity.get_available_contexts()
    assert current == 'docker-desktop'
    assert contexts == ['docker-desktop', 'minikube', 'kind-kind']


def test_get_available_contexts_failure(monkeypatch):
    def _fail(*a, **kw):
        raise FileNotFoundError("kubectl not found")
    monkeypatch.setattr(subprocess, 'run', _fail)
    current, contexts = k8s_connectivity.get_available_contexts()
    assert current == ""
    assert contexts == []


# ---------------------------------------------------------------------------
# switch_context()
# ---------------------------------------------------------------------------

def test_switch_context_success(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0),
    )
    assert k8s_connectivity.switch_context("minikube") is True


def test_switch_context_failure(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1),
    )
    assert k8s_connectivity.switch_context("bad-ctx") is False


# ---------------------------------------------------------------------------
# is_minikube_installed()
# ---------------------------------------------------------------------------

def test_is_minikube_installed_true(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0, stdout='v1.32.0'),
    )
    assert k8s_connectivity.is_minikube_installed() is True


def test_is_minikube_installed_not_found(monkeypatch):
    def _not_found(*a, **kw):
        raise FileNotFoundError("minikube not found")
    monkeypatch.setattr(subprocess, 'run', _not_found)
    assert k8s_connectivity.is_minikube_installed() is False


# ---------------------------------------------------------------------------
# is_minikube_running()
# ---------------------------------------------------------------------------

def test_is_minikube_running_true(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0, stdout='Running'),
    )
    assert k8s_connectivity.is_minikube_running() is True


def test_is_minikube_running_false(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1, stdout='Stopped'),
    )
    assert k8s_connectivity.is_minikube_running() is False


# ---------------------------------------------------------------------------
# try_minikube_start()
# ---------------------------------------------------------------------------

def test_try_minikube_start_not_installed(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: False)
    assert k8s_connectivity.try_minikube_start() is False


def test_try_minikube_start_already_running(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: True)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: True)
    monkeypatch.setattr(k8s_connectivity, 'switch_context', lambda ctx: True)
    monkeypatch.setattr(k8s_connectivity, 'context_reachable', lambda ctx, **kw: True)
    assert k8s_connectivity.try_minikube_start() is True


def test_try_minikube_start_starts_and_succeeds(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: True)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'switch_context', lambda ctx: True)
    monkeypatch.setattr(k8s_connectivity, 'context_reachable', lambda ctx, **kw: True)
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0),
    )
    output = []
    assert k8s_connectivity.try_minikube_start(print_fn=lambda *a, **kw: output.append(str(a))) is True


def test_try_minikube_start_fails(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: True)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'context_reachable', lambda ctx, **kw: False)
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1, stderr='error starting'),
    )
    output = []
    assert k8s_connectivity.try_minikube_start(print_fn=lambda *a, **kw: output.append(str(a))) is False


# ---------------------------------------------------------------------------
# probe_api_port()
# ---------------------------------------------------------------------------

def test_probe_api_port_closed(monkeypatch):
    import socket
    original_create = socket.create_connection
    def _fail(*a, **kw):
        raise OSError("Connection refused")
    monkeypatch.setattr(socket, 'create_connection', _fail)
    assert k8s_connectivity.probe_api_port('127.0.0.1', 6443) == 'closed'


# ---------------------------------------------------------------------------
# attempt_recovery()
# ---------------------------------------------------------------------------

def test_attempt_recovery_no_contexts(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'get_available_contexts', lambda: ("", []))
    output = []
    assert k8s_connectivity.attempt_recovery(print_fn=lambda *a, **kw: output.append(str(a))) is False


def test_attempt_recovery_finds_reachable_context(monkeypatch):
    monkeypatch.setattr(
        k8s_connectivity, 'get_available_contexts',
        lambda: ("docker-desktop", ["docker-desktop", "minikube"]),
    )
    monkeypatch.setattr(k8s_connectivity, 'context_reachable', lambda ctx, **kw: ctx == "minikube")
    monkeypatch.setattr(k8s_connectivity, 'switch_context', lambda ctx: True)
    monkeypatch.setattr(k8s_connectivity, 'try_minikube_start', lambda **kw: False)
    output = []
    assert k8s_connectivity.attempt_recovery(print_fn=lambda *a, **kw: output.append(str(a))) is True


def test_attempt_recovery_falls_back_to_minikube_for_minikube_context(monkeypatch):
    monkeypatch.setattr(
        k8s_connectivity, 'get_available_contexts',
        lambda: ("minikube", ["minikube"]),
    )
    monkeypatch.setattr(k8s_connectivity, 'build_recommendations', lambda: [
        {'action': 'start_minikube', 'label': 'Start minikube cluster', 'detail': 'minikube start'},
    ])
    monkeypatch.setattr(k8s_connectivity, 'apply_recommendation', lambda action, print_fn=None: action == 'start_minikube')
    output = []
    assert k8s_connectivity.attempt_recovery(print_fn=lambda *a, **kw: output.append(str(a))) is True


def test_attempt_recovery_does_not_cross_to_minikube_from_docker_desktop(monkeypatch):
    monkeypatch.setattr(
        k8s_connectivity, 'get_available_contexts',
        lambda: ("docker-desktop", ["docker-desktop"]),
    )
    monkeypatch.setattr(k8s_connectivity, 'build_recommendations', lambda: [])
    monkeypatch.setattr(k8s_connectivity, 'try_minikube_start', lambda **kw: True)
    output = []
    assert k8s_connectivity.attempt_recovery(print_fn=lambda *a, **kw: output.append(str(a))) is False


def test_attempt_recovery_all_fail(monkeypatch):
    monkeypatch.setattr(
        k8s_connectivity, 'get_available_contexts',
        lambda: ("docker-desktop", ["docker-desktop", "kind-kind"]),
    )
    monkeypatch.setattr(k8s_connectivity, 'context_reachable', lambda ctx, **kw: False)
    monkeypatch.setattr(k8s_connectivity, 'build_recommendations', lambda: [])
    output = []
    assert k8s_connectivity.attempt_recovery(print_fn=lambda *a, **kw: output.append(str(a))) is False


# ---------------------------------------------------------------------------
# diagnose_and_recover()
# ---------------------------------------------------------------------------

def test_diagnose_and_recover_already_reachable(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'k8s_reachable', lambda: "reachable")
    output = []
    assert k8s_connectivity.diagnose_and_recover(print_fn=lambda *a, **kw: output.append(str(a))) is True


def test_diagnose_and_recover_docker_down(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'k8s_reachable', lambda: "unreachable")
    monkeypatch.setattr(k8s_connectivity, '_docker_running', lambda: False)
    output = []
    assert k8s_connectivity.diagnose_and_recover(print_fn=lambda *a, **kw: output.append(str(a))) is False


def test_diagnose_and_recover_succeeds_after_recovery(monkeypatch):
    call_count = [0]
    def _reachable():
        call_count[0] += 1
        return "reachable" if call_count[0] > 1 else "unreachable"
    monkeypatch.setattr(k8s_connectivity, 'k8s_reachable', _reachable)
    monkeypatch.setattr(k8s_connectivity, '_docker_running', lambda: True)
    monkeypatch.setattr(k8s_connectivity, 'attempt_recovery', lambda **kw: True)
    output = []
    assert k8s_connectivity.diagnose_and_recover(print_fn=lambda *a, **kw: output.append(str(a))) is True


def test_diagnose_and_recover_recovery_fails(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'k8s_reachable', lambda: "unreachable")
    monkeypatch.setattr(k8s_connectivity, '_docker_running', lambda: True)
    monkeypatch.setattr(k8s_connectivity, 'attempt_recovery', lambda **kw: False)
    monkeypatch.setattr(k8s_connectivity, 'collect_failure_details', lambda: [])
    output = []
    assert k8s_connectivity.diagnose_and_recover(print_fn=lambda *a, **kw: output.append(str(a))) is False


# ---------------------------------------------------------------------------
# collect_failure_details() — basic structure test
# ---------------------------------------------------------------------------

def test_collect_failure_details_returns_list(monkeypatch):
    # Stub out all subprocess calls to avoid real system probes
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1, stdout='', stderr=''),
    )
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: False)
    # Mock probe_api_port so it doesn't open a real socket
    monkeypatch.setattr(k8s_connectivity, 'probe_api_port', lambda h, p, **kw: 'closed')
    # Mock os.path.exists to skip Docker Desktop settings file
    monkeypatch.setattr(k8s_connectivity.os.path, 'exists', lambda p: False)
    details = k8s_connectivity.collect_failure_details()
    assert isinstance(details, list)
    # Should at least have Docker process check and port check
    labels = [d[0] for d in details]
    assert any('Docker' in l for l in labels)


# ---------------------------------------------------------------------------
# format_failure_details()
# ---------------------------------------------------------------------------

def test_format_failure_details_empty():
    assert k8s_connectivity.format_failure_details([]) == ""


def test_format_failure_details_formats_rows():
    details = [
        ('Docker Desktop process', 'running', False),
        ('K8s API port', 'not listening', True),
    ]
    output = k8s_connectivity.format_failure_details(details)
    assert 'Docker Desktop process' in output
    assert 'not listening' in output
    assert 'Diagnostic details:' in output


# ---------------------------------------------------------------------------
# is_kind_installed()
# ---------------------------------------------------------------------------

def test_is_kind_installed_true(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0, stdout='kind v0.20.0'),
    )
    assert k8s_connectivity.is_kind_installed() is True


def test_is_kind_installed_not_found(monkeypatch):
    def _not_found(*a, **kw):
        raise FileNotFoundError("kind not found")
    monkeypatch.setattr(subprocess, 'run', _not_found)
    assert k8s_connectivity.is_kind_installed() is False


def test_is_kind_installed_timeout(monkeypatch):
    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd='kind', timeout=3)
    monkeypatch.setattr(subprocess, 'run', _timeout)
    assert k8s_connectivity.is_kind_installed() is False


# ---------------------------------------------------------------------------
# get_kind_containers()
# ---------------------------------------------------------------------------

def test_get_kind_containers_returns_parsed(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(
            returncode=0,
            stdout='kind-control-plane:Up 2 hours\nkind-worker:Exited (0) 1 hour ago\n',
        ),
    )
    result = k8s_connectivity.get_kind_containers()
    assert 'kind-control-plane' in result
    assert result['kind-control-plane']['running'] is True
    assert 'kind-worker' in result
    assert result['kind-worker']['running'] is False


def test_get_kind_containers_empty_on_failure(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1),
    )
    assert k8s_connectivity.get_kind_containers() == {}


def test_get_kind_containers_empty_on_exception(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("docker not available")
    monkeypatch.setattr(subprocess, 'run', _boom)
    assert k8s_connectivity.get_kind_containers() == {}


def test_get_kind_containers_skips_invalid_names(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(
            returncode=0,
            stdout='valid-name:Up 1 hour\n../../bad:Exited\n',
        ),
    )
    result = k8s_connectivity.get_kind_containers()
    assert 'valid-name' in result
    assert '../../bad' not in result


def test_get_kind_containers_handles_no_colon(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0, stdout='no-colon-here\n'),
    )
    assert k8s_connectivity.get_kind_containers() == {}


# ---------------------------------------------------------------------------
# try_kind_restart()
# ---------------------------------------------------------------------------

def test_try_kind_restart_starts_stopped_containers(monkeypatch):
    monkeypatch.setattr(
        k8s_connectivity, 'get_kind_containers',
        lambda: {'kind-control-plane': {'running': False, 'status': 'Exited'}},
    )
    started = []
    def _run(cmd, **kw):
        if cmd[0] == 'docker' and cmd[1] == 'start':
            started.append(cmd[2])
            return _CmdResult(returncode=0)
        return _CmdResult(returncode=0)
    monkeypatch.setattr(subprocess, 'run', _run)
    monkeypatch.setattr(k8s_connectivity.time, 'sleep', lambda s: None)
    monkeypatch.setattr(
        k8s_connectivity, 'get_available_contexts',
        lambda: ('kind-kind', ['kind-kind']),
    )
    monkeypatch.setattr(k8s_connectivity, 'context_reachable', lambda ctx: True)
    monkeypatch.setattr(k8s_connectivity, 'switch_context', lambda ctx: None)

    output = []
    assert k8s_connectivity.try_kind_restart(print_fn=lambda *a, **kw: output.append(str(a))) is True
    assert 'kind-control-plane' in started


def test_try_kind_restart_no_stopped_returns_false(monkeypatch):
    monkeypatch.setattr(
        k8s_connectivity, 'get_kind_containers',
        lambda: {'kind-control-plane': {'running': True, 'status': 'Up 2 hours'}},
    )
    assert k8s_connectivity.try_kind_restart() is False


def test_try_kind_restart_unreachable_after_start(monkeypatch):
    monkeypatch.setattr(
        k8s_connectivity, 'get_kind_containers',
        lambda: {'kind-control-plane': {'running': False, 'status': 'Exited'}},
    )
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0),
    )
    monkeypatch.setattr(k8s_connectivity.time, 'sleep', lambda s: None)
    monkeypatch.setattr(
        k8s_connectivity, 'get_available_contexts',
        lambda: ('kind-kind', ['kind-kind']),
    )
    monkeypatch.setattr(k8s_connectivity, 'context_reachable', lambda ctx: False)

    assert k8s_connectivity.try_kind_restart(print_fn=lambda *a, **kw: None) is False


# ---------------------------------------------------------------------------
# build_recommendations()
# ---------------------------------------------------------------------------

def test_build_recommendations_stopped_kind(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'get_available_contexts', lambda: ('kind-kind', ['kind-kind']))
    monkeypatch.setattr(
        k8s_connectivity, 'get_kind_containers',
        lambda: {'kind-control-plane': {'running': False, 'status': 'Exited'}},
    )
    monkeypatch.setattr(k8s_connectivity, 'get_kind_clusters', lambda: ['kind'])
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_kind_installed', lambda: True)
    recs = k8s_connectivity.build_recommendations()
    assert len(recs) >= 1
    assert recs[0]['action'] == 'start_kind'


def test_build_recommendations_minikube_stopped(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'get_available_contexts', lambda: ('minikube', ['minikube']))
    monkeypatch.setattr(k8s_connectivity, 'get_kind_containers', lambda: {})
    monkeypatch.setattr(k8s_connectivity, 'get_kind_clusters', lambda: [])
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: True)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_kind_installed', lambda: False)
    recs = k8s_connectivity.build_recommendations()
    assert any(r['action'] == 'start_minikube' for r in recs)


def test_build_recommendations_create_kind(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'get_available_contexts', lambda: ('', []))
    monkeypatch.setattr(k8s_connectivity, 'get_kind_containers', lambda: {})
    monkeypatch.setattr(k8s_connectivity, 'get_kind_clusters', lambda: [])
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_kind_installed', lambda: True)
    recs = k8s_connectivity.build_recommendations()
    assert any(r['action'] == 'create_kind' for r in recs)


def test_build_recommendations_empty_when_nothing_available(monkeypatch):
    monkeypatch.setattr('sys.platform', 'linux')
    monkeypatch.setattr(k8s_connectivity, 'get_available_contexts', lambda: ('docker-desktop', ['docker-desktop']))
    monkeypatch.setattr(k8s_connectivity, 'get_kind_containers', lambda: {})
    monkeypatch.setattr(k8s_connectivity, 'get_kind_clusters', lambda: [])
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_kind_installed', lambda: False)
    assert k8s_connectivity.build_recommendations() == []


def test_build_recommendations_priority_order(monkeypatch):
    """Kind fix comes before minikube when both available."""
    monkeypatch.setattr(k8s_connectivity, 'get_available_contexts', lambda: ('', []))
    monkeypatch.setattr(
        k8s_connectivity, 'get_kind_containers',
        lambda: {'kind-cp': {'running': False, 'status': 'Exited'}},
    )
    monkeypatch.setattr(k8s_connectivity, 'get_kind_clusters', lambda: [])
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: True)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_kind_installed', lambda: True)
    recs = k8s_connectivity.build_recommendations()
    actions = [r['action'] for r in recs]
    assert actions.index('start_kind') < actions.index('start_minikube')


def test_build_recommendations_kind_context_does_not_offer_minikube(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'get_available_contexts', lambda: ('kind-kind', ['kind-kind', 'minikube']))
    monkeypatch.setattr(
        k8s_connectivity, 'get_kind_containers',
        lambda: {'kind-control-plane': {'running': True, 'status': 'Up 5m'}},
    )
    monkeypatch.setattr(k8s_connectivity, 'get_kind_clusters', lambda: ['kind'])
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: True)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_kind_installed', lambda: True)

    recs = k8s_connectivity.build_recommendations()

    assert all(rec['action'] != 'start_minikube' for rec in recs)


def test_cluster_runtime_status_kind_running(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'get_available_contexts', lambda: ('kind-kind', ['kind-kind']))
    monkeypatch.setattr(
        k8s_connectivity, 'get_kind_containers',
        lambda: {'kind-control-plane': {'running': True, 'status': 'Up 5m'}},
    )
    monkeypatch.setattr(k8s_connectivity, 'get_kind_clusters', lambda: ['kind'])
    assert k8s_connectivity.cluster_runtime_status() == 'kind running'


def test_cluster_runtime_status_docker_desktop_selected(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'get_available_contexts', lambda: ('docker-desktop', ['docker-desktop']))
    monkeypatch.setattr(k8s_connectivity, 'get_kind_containers', lambda: {})
    monkeypatch.setattr(k8s_connectivity, 'get_kind_clusters', lambda: [])
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_kind_installed', lambda: False)
    assert k8s_connectivity.cluster_runtime_status() == 'docker-desktop selected'


# ---------------------------------------------------------------------------
# apply_recommendation()
# ---------------------------------------------------------------------------

def test_apply_recommendation_start_kind(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'try_kind_restart', lambda print_fn=None: True)
    assert k8s_connectivity.apply_recommendation('start_kind') is True


def test_apply_recommendation_start_minikube(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'try_minikube_start', lambda print_fn=None: True)
    assert k8s_connectivity.apply_recommendation('start_minikube') is True


def test_apply_recommendation_create_kind_success(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=0, stdout='Creating cluster'),
    )
    monkeypatch.setattr(k8s_connectivity.time, 'sleep', lambda s: None)
    monkeypatch.setattr(k8s_connectivity, 'context_reachable', lambda ctx: True)
    monkeypatch.setattr(k8s_connectivity, 'switch_context', lambda ctx: None)
    assert k8s_connectivity.apply_recommendation('create_kind', print_fn=lambda *a, **kw: None) is True


def test_apply_recommendation_create_kind_failure(monkeypatch):
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _CmdResult(returncode=1, stderr='error creating cluster'),
    )
    monkeypatch.setattr(k8s_connectivity.time, 'sleep', lambda s: None)
    assert k8s_connectivity.apply_recommendation('create_kind', print_fn=lambda *a, **kw: None) is False


def test_apply_recommendation_unknown_action():
    assert k8s_connectivity.apply_recommendation('unknown_action') is False


# ---------------------------------------------------------------------------
# diagnose()
# ---------------------------------------------------------------------------

def test_diagnose_healthy(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'k8s_reachable', lambda: 'reachable')
    monkeypatch.setattr(
        k8s_connectivity, 'get_available_contexts',
        lambda: ('kind-kind', ['kind-kind', 'minikube']),
    )
    monkeypatch.setattr(k8s_connectivity, 'get_kind_containers', lambda: {})
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: True)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: True)
    result = k8s_connectivity.diagnose()
    assert result['status'] == 'healthy'
    assert result['running_cluster'] == 'kind-kind'
    assert result['recommendations'] == []
    assert 'kind-kind' in result['contexts_available']


def test_diagnose_unreachable_with_recommendations(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'k8s_reachable', lambda: 'unreachable')
    monkeypatch.setattr(
        k8s_connectivity, 'get_available_contexts',
        lambda: ('kind-kind', ['kind-kind']),
    )
    monkeypatch.setattr(k8s_connectivity, 'context_reachable', lambda ctx: False)
    monkeypatch.setattr(
        k8s_connectivity, 'get_kind_containers',
        lambda: {'kind-cp': {'running': False, 'status': 'Exited'}},
    )
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_kind_installed', lambda: True)
    # Stub collect_failure_details to avoid subprocess calls
    monkeypatch.setattr(k8s_connectivity, 'collect_failure_details', lambda: [('test', 'val', False)])
    result = k8s_connectivity.diagnose()
    assert result['status'] == 'unreachable'
    assert result['running_cluster'] is None
    assert len(result['recommendations']) >= 1
    assert result['recommendations'][0]['action'] == 'start_kind'
    assert len(result['details']) >= 1


def test_diagnose_finds_alternate_reachable_context(monkeypatch):
    monkeypatch.setattr(k8s_connectivity, 'k8s_reachable', lambda: 'unreachable')
    monkeypatch.setattr(
        k8s_connectivity, 'get_available_contexts',
        lambda: ('docker-desktop', ['docker-desktop', 'minikube']),
    )
    monkeypatch.setattr(
        k8s_connectivity, 'context_reachable',
        lambda ctx: ctx == 'minikube',
    )
    monkeypatch.setattr(k8s_connectivity, 'get_kind_containers', lambda: {})
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: True)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: True)
    result = k8s_connectivity.diagnose()
    assert result['status'] == 'healthy'
    assert result['running_cluster'] == 'minikube'


def test_diagnose_no_contexts(monkeypatch):
    monkeypatch.setattr('sys.platform', 'linux')
    monkeypatch.setattr(k8s_connectivity, 'k8s_reachable', lambda: 'unreachable')
    monkeypatch.setattr(k8s_connectivity, 'get_available_contexts', lambda: ('', []))
    monkeypatch.setattr(k8s_connectivity, 'get_kind_containers', lambda: {})
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_installed', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_minikube_running', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'is_kind_installed', lambda: False)
    monkeypatch.setattr(k8s_connectivity, 'collect_failure_details', lambda: [])
    result = k8s_connectivity.diagnose()
    assert result['status'] == 'unreachable'
    assert result['contexts_available'] == []
    assert result['recommendations'] == []
