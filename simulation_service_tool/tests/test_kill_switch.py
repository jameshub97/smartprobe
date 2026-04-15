"""Tests for the kill switch service and welcome menu wiring."""

from unittest.mock import patch, MagicMock
import types
import pytest

from simulation_service_tool.services.kill_switch import (
    get_active_pods,
    kill_all_pods,
    kill_simulation_pods,
    probe_kill_switch_targets,
    nuke_all,
)


def _cmd_result(returncode=0, stdout="", stderr=""):
    r = types.SimpleNamespace()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# ── get_active_pods ────────────────────────────────────────────────

class TestGetActivePods:
    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_pods_found(self, mock_cmd):
        lines = (
            "web-abc-123       1/1   Running\n"
            "worker-xyz-456    1/1   Running\n"
            "init-pod-789      0/1   Pending\n"
        )
        mock_cmd.return_value = _cmd_result(0, lines)
        pods = get_active_pods()
        assert len(pods) == 3
        assert pods[0]["name"] == "web-abc-123"
        assert pods[0]["status"] == "Running"
        assert pods[2]["status"] == "Pending"

    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_no_pods(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "")
        assert get_active_pods() == []

    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_error(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, "")
        assert get_active_pods() == []

    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_custom_namespace(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "")
        get_active_pods(namespace="monitoring")
        call_args = mock_cmd.call_args
        assert call_args[1]["namespace"] == "monitoring"


# ── kill_all_pods ──────────────────────────────────────────────────

class TestKillAllPods:
    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_success(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd_result(0, "pod1  1/1  Running\npod2  1/1  Running\n"),  # get_active_pods
            _cmd_result(0, "pod1 deleted\npod2 deleted\n"),  # delete
        ]
        result = kill_all_pods()
        assert result["success"] is True
        assert result["deleted"] == 2
        assert result["errors"] == []

    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_no_pods(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "")
        result = kill_all_pods()
        assert result["success"] is True
        assert result["deleted"] == 0

    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_delete_fails(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd_result(0, "pod1  1/1  Running\n"),  # get_active_pods
            _cmd_result(1, stderr="forbidden"),  # delete
        ]
        result = kill_all_pods()
        assert result["success"] is False
        assert "forbidden" in result["errors"][0]


# ── kill_simulation_pods ───────────────────────────────────────────

class TestKillSimulationPods:
    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_success(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd_result(0, "pw-agent-1  1/1  Running\n"),  # get labeled pods
            _cmd_result(0, "deleted"),  # delete
        ]
        result = kill_simulation_pods()
        assert result["success"] is True
        assert result["deleted"] == 1

    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_no_simulation_pods(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "")
        result = kill_simulation_pods()
        assert result["success"] is True
        assert result["deleted"] == 0

    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_delete_fails(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd_result(0, "pw-agent-1  1/1  Running\n"),  # get
            _cmd_result(1, stderr="timeout"),  # delete
        ]
        result = kill_simulation_pods()
        assert result["success"] is False
        assert "timeout" in result["errors"][0]


# ── nuke_all ───────────────────────────────────────────────────────

class TestNukeAll:
    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_full_nuke(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd_result(0, "release-a\nrelease-b\n"),  # helm list
            _cmd_result(0, "uninstalled"),  # helm uninstall release-a
            _cmd_result(0, "uninstalled"),  # helm uninstall release-b
            _cmd_result(0, "pod1  1/1  Running\n"),  # get_active_pods
            _cmd_result(0, "deleted"),  # delete pods
        ]
        result = nuke_all()
        assert result["releases_removed"] == 2
        assert result["pods_deleted"] == 1
        assert result["errors"] == []

    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_no_releases_no_pods(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd_result(0, ""),  # helm list empty
            _cmd_result(0, ""),  # get_active_pods empty
        ]
        result = nuke_all()
        assert result["releases_removed"] == 0
        assert result["pods_deleted"] == 0
        assert result["errors"] == []

    @patch("simulation_service_tool.services.kill_switch.run_cli_command")
    def test_helm_uninstall_partial_failure(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd_result(0, "rel-a\nrel-b\n"),  # helm list
            _cmd_result(0, "ok"),  # uninstall rel-a
            _cmd_result(1, stderr="stuck"),  # uninstall rel-b fails
            _cmd_result(0, ""),  # get_active_pods
        ]
        result = nuke_all()
        assert result["releases_removed"] == 1
        assert len(result["errors"]) == 1
        assert "rel-b" in result["errors"][0]


class TestProbeKillSwitchTargets:
    @patch("simulation_service_tool.services.kill_switch.list_helm_releases", return_value=["release-a"])
    @patch("simulation_service_tool.services.kill_switch.get_active_pods", return_value=[
        {"name": "pod-a", "ready": "1/1", "status": "Running"},
    ])
    def test_probe_counts_targets(self, mock_pods, mock_releases):
        progress = []
        result = probe_kill_switch_targets(progress_callback=progress.append)
        assert result["pod_count"] == 1
        assert result["release_count"] == 1
        assert result["has_targets"] is True
        assert progress[0].startswith("Checking active pods")
        assert progress[-1] == "Kill switch probe complete."


# ── welcome menu wiring ──────────────────────────────────────────

class TestWelcomeMenuKillSwitchWiring:
    def test_kill_switch_imported(self):
        from simulation_service_tool.menus import welcome
        assert hasattr(welcome, 'kill_all_pods')
        assert hasattr(welcome, 'nuke_all')
        assert hasattr(welcome, 'probe_kill_switch_targets')

    def test_kill_switch_in_handle_choice(self):
        import inspect
        from simulation_service_tool.menus.welcome import _handle_welcome_choice
        source = inspect.getsource(_handle_welcome_choice)
        assert '_kill_switch_action' in source

    def test_kill_switch_action_exists(self):
        from simulation_service_tool.menus.welcome import _kill_switch_action
        assert callable(_kill_switch_action)

    @patch("simulation_service_tool.menus.welcome.show_loading_spinner", return_value={
        "pods": [],
        "releases": [],
        "pod_count": 0,
        "release_count": 0,
        "has_targets": False,
    })
    @patch("simulation_service_tool.menus.welcome.questionary")
    def test_kill_switch_no_targets(self, mock_q, mock_spinner, capsys):
        mock_q.select.return_value.ask.return_value = "back"
        mock_q.Choice = MagicMock(side_effect=lambda **kw: kw.get("title", ""))
        from simulation_service_tool.menus.welcome import _kill_switch_action
        _kill_switch_action()
        out = capsys.readouterr().out
        assert "Probe complete: 0 active pod(s), 0 Helm release(s)." in out
        assert "No active pods or Helm releases found" in out
        mock_spinner.assert_called_once()

    @patch("simulation_service_tool.menus.welcome.nuke_all", return_value={"releases_removed": 2, "pods_deleted": 3, "errors": []})
    @patch("simulation_service_tool.menus.welcome.show_loading_spinner", return_value={
        "pods": [
            {"name": "pod-a", "ready": "1/1", "status": "Running"},
            {"name": "pod-b", "ready": "1/1", "status": "Running"},
        ],
        "releases": ["release-a", "release-b"],
        "pod_count": 2,
        "release_count": 2,
        "has_targets": True,
    })
    @patch("simulation_service_tool.menus.welcome.questionary")
    def test_kill_switch_nuke(self, mock_q, mock_spinner, mock_nuke, capsys):
        mock_q.select.side_effect = [
            MagicMock(ask=MagicMock(return_value="nuke")),  # confirm
            MagicMock(ask=MagicMock(return_value="back")),  # go back
        ]
        mock_q.Choice = MagicMock(side_effect=lambda **kw: kw.get("title", ""))
        from simulation_service_tool.menus.welcome import _kill_switch_action
        _kill_switch_action()
        out = capsys.readouterr().out
        assert "pod-a" in out
        assert "pod-b" in out
        assert "release-a" in out
        assert "release-b" in out
        mock_nuke.assert_called_once()

    @patch("simulation_service_tool.menus.welcome.kill_all_pods", return_value={"success": True, "deleted": 2, "errors": []})
    @patch("simulation_service_tool.menus.welcome.show_loading_spinner", return_value={
        "pods": [
            {"name": "pod-a", "ready": "1/1", "status": "Running"},
        ],
        "releases": [],
        "pod_count": 1,
        "release_count": 0,
        "has_targets": True,
    })
    @patch("simulation_service_tool.menus.welcome.questionary")
    def test_kill_switch_pods_only(self, mock_q, mock_spinner, mock_kill, capsys):
        mock_q.select.side_effect = [
            MagicMock(ask=MagicMock(return_value="pods")),
            MagicMock(ask=MagicMock(return_value="back")),
        ]
        mock_q.Choice = MagicMock(side_effect=lambda **kw: kw.get("title", ""))
        from simulation_service_tool.menus.welcome import _kill_switch_action
        _kill_switch_action()
        mock_kill.assert_called_once()

    @patch("simulation_service_tool.menus.welcome.nuke_all", return_value={"releases_removed": 1, "pods_deleted": 0, "errors": []})
    @patch("simulation_service_tool.menus.welcome.show_loading_spinner", return_value={
        "pods": [],
        "releases": ["release-a"],
        "pod_count": 0,
        "release_count": 1,
        "has_targets": True,
    })
    @patch("simulation_service_tool.menus.welcome.questionary")
    def test_kill_switch_releases_only(self, mock_q, mock_spinner, mock_nuke, capsys):
        mock_q.select.side_effect = [
            MagicMock(ask=MagicMock(return_value="nuke")),
            MagicMock(ask=MagicMock(return_value="back")),
        ]
        mock_q.Choice = MagicMock(side_effect=lambda **kw: kw.get("title", ""))
        from simulation_service_tool.menus.welcome import _kill_switch_action
        _kill_switch_action()
        out = capsys.readouterr().out
        assert "release-a" in out
        assert "Removing Helm releases" in out
        mock_nuke.assert_called_once()

    @patch("simulation_service_tool.menus.welcome.show_loading_spinner", return_value={
        "pods": [
            {"name": "pod-a", "ready": "1/1", "status": "Running"},
        ],
        "releases": [],
        "pod_count": 1,
        "release_count": 0,
        "has_targets": True,
    })
    @patch("simulation_service_tool.menus.welcome.questionary")
    def test_kill_switch_cancel(self, mock_q, mock_spinner, capsys):
        mock_q.select.side_effect = [
            MagicMock(ask=MagicMock(return_value="cancel")),
            MagicMock(ask=MagicMock(return_value="back")),
        ]
        mock_q.Choice = MagicMock(side_effect=lambda **kw: kw.get("title", ""))
        from simulation_service_tool.menus.welcome import _kill_switch_action
        _kill_switch_action()
        out = capsys.readouterr().out
        assert "Cancelled" in out
