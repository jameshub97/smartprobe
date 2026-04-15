"""Tests for cli/main.py startup diagnostic helpers."""

import socket
import threading

import pytest

from simulation_service_tool.services.k8s_connectivity import probe_api_port as _probe_api_port


# ---------------------------------------------------------------------------
# Helpers — tiny TCP servers for each scenario
# ---------------------------------------------------------------------------

def _start_server(handler, host='127.0.0.1', port=0):
    """Bind a one-shot server on a random port, return (thread, port)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    actual_port = srv.getsockname()[1]

    def run():
        try:
            conn, _ = srv.accept()
            handler(conn)
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, actual_port


# ---------------------------------------------------------------------------
# _probe_api_port tests
# ---------------------------------------------------------------------------

def test_probe_api_port_closed_returns_closed():
    """Nothing listening on the port → 'closed'."""
    # Find an unused port by binding then immediately closing
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()

    assert _probe_api_port('127.0.0.1', port) == 'closed'


def test_probe_api_port_eof_returns_eof():
    """Server accepts then immediately closes — matches Docker Desktop k8s crash."""
    def close_immediately(conn):
        conn.close()

    _, port = _start_server(close_immediately)
    assert _probe_api_port('127.0.0.1', port) == 'eof'


def test_probe_api_port_listening_when_data_sent():
    """Server accepts and sends data (simulates TLS hello) → 'listening'."""
    def send_byte(conn):
        conn.sendall(b'\x16')  # TLS record type byte
        conn.close()

    _, port = _start_server(send_byte)
    assert _probe_api_port('127.0.0.1', port) == 'listening'


def test_probe_api_port_listening_when_server_holds_silent():
    """Server accepts but stays silent (TLS handshake in progress) → 'listening'."""
    import time

    def hold_open(conn):
        time.sleep(2)  # Outlasts the 0.5 s probe timeout
        conn.close()

    _, port = _start_server(hold_open)
    assert _probe_api_port('127.0.0.1', port, timeout=0.3) == 'listening'


# ---------------------------------------------------------------------------
# Startup prerequisite helpers
# ---------------------------------------------------------------------------

def test_docker_api_status_reachable(monkeypatch):
    from simulation_service_tool.cli.main import _docker_api_status
    from simulation_service_tool.services import smart_diagnostics

    monkeypatch.setattr(smart_diagnostics, '_docker_running', lambda: True)
    assert _docker_api_status() == 'reachable'


def test_docker_api_status_unreachable(monkeypatch):
    from simulation_service_tool.cli.main import _docker_api_status
    from simulation_service_tool.services import smart_diagnostics

    monkeypatch.setattr(smart_diagnostics, '_docker_running', lambda: False)
    assert _docker_api_status() == 'unreachable'


def test_cluster_runtime_status_passthrough(monkeypatch):
    from simulation_service_tool.cli.main import _cluster_runtime_status
    from simulation_service_tool.services import k8s_connectivity

    monkeypatch.setattr(k8s_connectivity, 'cluster_runtime_status', lambda: 'kind running')
    assert _cluster_runtime_status() == 'kind running'


def test_runtime_checks_in_startup_checks():
    """Startup diagnostics should probe real runtime prerequisites."""
    from simulation_service_tool.cli.main import _CHECKS

    labels = [label for label, _ in _CHECKS]
    assert 'Docker API' in labels
    assert 'Cluster runtime' in labels
    assert 'Kubernetes API' in labels
    assert 'docker-compose.yml' not in labels
