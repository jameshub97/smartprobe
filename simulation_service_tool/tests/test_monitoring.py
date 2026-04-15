"""Tests for the monitoring service and monitoring menu."""

from unittest.mock import patch, MagicMock
import types
import pytest

from simulation_service_tool.services.monitoring import (
    is_helm_available,
    is_monitoring_installed,
    install_stack,
    upgrade_stack,
    uninstall_stack,
    get_stack_status,
    get_monitoring_pods,
    get_grafana_access,
    get_prometheus_targets,
    apply_servicemonitor,
    RELEASE_NAME,
    NAMESPACE,
)


def _cmd_result(returncode=0, stdout="", stderr=""):
    r = types.SimpleNamespace()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# ── is_helm_available ──────────────────────────────────────────────

class TestIsHelmAvailable:
    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_available(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "v3.12.0")
        assert is_helm_available() is True

    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_not_available(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, "")
        assert is_helm_available() is False


# ── is_monitoring_installed ────────────────────────────────────────

class TestIsMonitoringInstalled:
    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_installed(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "STATUS: deployed")
        assert is_monitoring_installed() is True

    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_not_installed(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, "")
        assert is_monitoring_installed() is False


# ── install_stack ──────────────────────────────────────────────────

class TestInstallStack:
    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_success(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "deployed")
        result = install_stack()
        assert result["success"] is True
        # repo add, repo update, install = 3 calls
        assert mock_cmd.call_count == 3

    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_failure(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd_result(0),  # repo add
            _cmd_result(0),  # repo update
            _cmd_result(1, stderr="timeout"),  # install
        ]
        result = install_stack()
        assert result["success"] is False


# ── upgrade_stack ──────────────────────────────────────────────────

class TestUpgradeStack:
    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_success(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "upgraded")
        result = upgrade_stack()
        assert result["success"] is True

    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_failure(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd_result(0),  # repo update
            _cmd_result(1, stderr="err"),  # upgrade
        ]
        result = upgrade_stack()
        assert result["success"] is False


# ── uninstall_stack ────────────────────────────────────────────────

class TestUninstallStack:
    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_success(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "release removed")
        result = uninstall_stack()
        assert result["success"] is True

    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_failure(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, stderr="not found")
        result = uninstall_stack()
        assert result["success"] is False


# ── get_stack_status ───────────────────────────────────────────────

class TestGetStackStatus:
    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_installed(self, mock_cmd):
        import json
        payload = {"info": {"status": "deployed"}, "version": "1"}
        mock_cmd.return_value = _cmd_result(0, json.dumps(payload))
        status = get_stack_status()
        assert status["installed"] is True
        assert status["status"] == "deployed"

    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_not_installed(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, "")
        status = get_stack_status()
        assert status["installed"] is False


# ── get_monitoring_pods ────────────────────────────────────────────

class TestGetMonitoringPods:
    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_pods_found(self, mock_cmd):
        lines = (
            "prometheus-server-0   1/1   Running\n"
            "grafana-abc-123       1/1   Running\n"
        )
        mock_cmd.return_value = _cmd_result(0, lines)
        pods = get_monitoring_pods()
        assert len(pods) == 2
        assert pods[0]["name"] == "prometheus-server-0"
        assert pods[1]["status"] == "Running"

    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_no_pods(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "")
        pods = get_monitoring_pods()
        assert pods == []

    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_error(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, "")
        pods = get_monitoring_pods()
        assert pods == []


# ── get_grafana_access / get_prometheus_targets ────────────────────

class TestAccessHelpers:
    def test_grafana_access(self):
        info = get_grafana_access()
        assert "port-forward" in info["command"]
        assert info["url"].startswith("http://")
        assert info["credentials"]["username"] == "admin"

    def test_prometheus_targets(self):
        info = get_prometheus_targets()
        assert "port-forward" in info["command"]
        assert "9090" in info["url"]


# ── apply_servicemonitor ──────────────────────────────────────────

class TestApplyServiceMonitor:
    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_all_succeed(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "created")
        result = apply_servicemonitor()
        assert result["success"] is True
        assert len(result["results"]) == 3

    @patch("simulation_service_tool.services.monitoring.run_cli_command")
    def test_partial_failure(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd_result(0, "ok"),
            _cmd_result(1, stderr="CRD not found"),
            _cmd_result(0, "ok"),
        ]
        result = apply_servicemonitor()
        assert result["success"] is False
        assert result["results"][1]["success"] is False


# ── monitoring_menu ───────────────────────────────────────────────

class TestMonitoringMenu:
    @patch("simulation_service_tool.menus.monitoring.is_helm_available", return_value=False)
    @patch("simulation_service_tool.menus.monitoring.questionary")
    def test_no_helm_shows_warning(self, mock_q, mock_helm, capsys):
        mock_q.select.return_value.ask.return_value = "continue"
        mock_q.Choice = MagicMock(side_effect=lambda **kw: kw.get("title", ""))
        from simulation_service_tool.menus.monitoring import monitoring_menu
        monitoring_menu()
        out = capsys.readouterr().out
        assert "Helm" in out or "helm" in out.lower()

    @patch("simulation_service_tool.menus.monitoring.is_helm_available", return_value=True)
    @patch("simulation_service_tool.menus.monitoring.is_monitoring_installed", return_value=True)
    @patch("simulation_service_tool.menus.monitoring.get_stack_status", return_value={"installed": True, "status": "deployed", "namespace": "monitoring", "version": "1"})
    @patch("simulation_service_tool.menus.monitoring.questionary")
    def test_installed_shows_status(self, mock_q, mock_status, mock_installed, mock_helm, capsys):
        mock_q.select.return_value.ask.return_value = "back"
        mock_q.Separator = questionary_separator_stub
        mock_q.Choice = MagicMock(side_effect=lambda **kw: kw.get("title", ""))
        from simulation_service_tool.menus.monitoring import monitoring_menu
        monitoring_menu()
        out = capsys.readouterr().out
        assert "Installed" in out or "installed" in out.lower()


def questionary_separator_stub():
    return "---"


# ── welcome_menu wiring ──────────────────────────────────────────

class TestWelcomeMenuMonitoringWiring:
    def test_monitoring_menu_imported(self):
        from simulation_service_tool.menus import welcome
        assert hasattr(welcome, 'monitoring_menu')

    def test_monitoring_in_handle_choice_actions(self):
        """Ensure monitoring_menu is referenced in _handle_welcome_choice."""
        import inspect
        from simulation_service_tool.menus.welcome import _handle_welcome_choice
        source = inspect.getsource(_handle_welcome_choice)
        assert 'monitoring_menu' in source
