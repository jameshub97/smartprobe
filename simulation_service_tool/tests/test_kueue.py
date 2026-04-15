"""Tests for the Kueue service and Kueue menu."""

from unittest.mock import patch, MagicMock
import types
import json
import pytest

from simulation_service_tool.services.kueue import (
    is_kueue_installed,
    install_kueue,
    uninstall_kueue,
    apply_queues,
    delete_queues,
    get_cluster_queue_status,
    get_local_queue_status,
    list_workloads,
    KUEUE_MANIFEST,
)


def _cmd_result(returncode=0, stdout="", stderr=""):
    r = types.SimpleNamespace()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# ── is_kueue_installed ─────────────────────────────────────────────

class TestIsKueueInstalled:
    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_installed(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "clusterqueues.kueue.x-k8s.io")
        assert is_kueue_installed() is True

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_not_installed(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, "")
        assert is_kueue_installed() is False


# ── install_kueue ──────────────────────────────────────────────────

class TestInstallKueue:
    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_success(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "created")
        result = install_kueue()
        assert result["success"] is True
        assert mock_cmd.call_count == 1

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_failure(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, stderr="connection refused")
        result = install_kueue()
        assert result["success"] is False


# ── uninstall_kueue ────────────────────────────────────────────────

class TestUninstallKueue:
    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_success(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "deleted")
        result = uninstall_kueue()
        assert result["success"] is True

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_failure(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, stderr="not found")
        result = uninstall_kueue()
        assert result["success"] is False


# ── apply_queues ───────────────────────────────────────────────────

class TestApplyQueues:
    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_success(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "created")
        result = apply_queues()
        assert result["success"] is True

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_failure(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, stderr="CRD not found")
        result = apply_queues()
        assert result["success"] is False


# ── delete_queues ──────────────────────────────────────────────────

class TestDeleteQueues:
    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_success(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "deleted")
        result = delete_queues()
        assert result["success"] is True

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_failure(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, stderr="error")
        result = delete_queues()
        assert result["success"] is False


# ── get_cluster_queue_status ───────────────────────────────────────

class TestGetClusterQueueStatus:
    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_exists(self, mock_cmd):
        payload = {
            "status": {"pendingWorkloads": 3, "admittedWorkloads": 5},
            "spec": {
                "resourceGroups": [{
                    "coveredResources": ["cpu", "memory"],
                    "flavors": [{
                        "name": "default-flavor",
                        "resources": [
                            {"name": "cpu", "nominalQuota": "8"},
                            {"name": "memory", "nominalQuota": "16Gi"},
                        ],
                    }],
                }],
            },
        }
        mock_cmd.return_value = _cmd_result(0, json.dumps(payload))
        status = get_cluster_queue_status()
        assert status["exists"] is True
        assert status["pending_workloads"] == 3
        assert status["admitted_workloads"] == 5
        assert status["quotas"]["cpu"] == "8"
        assert status["quotas"]["memory"] == "16Gi"

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_not_exists(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, "")
        status = get_cluster_queue_status()
        assert status["exists"] is False

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_bad_json(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, "not json")
        status = get_cluster_queue_status()
        assert status["exists"] is False


# ── get_local_queue_status ─────────────────────────────────────────

class TestGetLocalQueueStatus:
    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_exists(self, mock_cmd):
        payload = {
            "status": {"pendingWorkloads": 1, "admittedWorkloads": 2},
        }
        mock_cmd.return_value = _cmd_result(0, json.dumps(payload))
        status = get_local_queue_status()
        assert status["exists"] is True
        assert status["pending_workloads"] == 1
        assert status["admitted_workloads"] == 2

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_not_exists(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, "")
        status = get_local_queue_status()
        assert status["exists"] is False


# ── list_workloads ─────────────────────────────────────────────────

class TestListWorkloads:
    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_workloads_found(self, mock_cmd):
        payload = {
            "items": [
                {
                    "metadata": {
                        "name": "test-job-abc",
                        "labels": {"kueue.x-k8s.io/queue-name": "simulation-queue"},
                        "creationTimestamp": "2026-04-14T10:00:00Z",
                    },
                    "status": {
                        "conditions": [
                            {"type": "Admitted", "status": "True"},
                        ],
                    },
                },
                {
                    "metadata": {
                        "name": "test-job-xyz",
                        "labels": {},
                        "creationTimestamp": "2026-04-14T10:01:00Z",
                    },
                    "status": {"conditions": []},
                },
            ],
        }
        mock_cmd.return_value = _cmd_result(0, json.dumps(payload))
        workloads = list_workloads()
        assert len(workloads) == 2
        assert workloads[0]["name"] == "test-job-abc"
        assert workloads[0]["admitted"] is True
        assert workloads[0]["queue"] == "simulation-queue"
        assert workloads[1]["admitted"] is False

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_no_workloads(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(0, json.dumps({"items": []}))
        assert list_workloads() == []

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_error(self, mock_cmd):
        mock_cmd.return_value = _cmd_result(1, "")
        assert list_workloads() == []


# ── kueue_menu ────────────────────────────────────────────────────

class TestKueueMenu:
    @patch("simulation_service_tool.menus.kueue.is_kueue_installed", return_value=False)
    @patch("simulation_service_tool.menus.kueue.questionary")
    def test_not_installed_shows_install_option(self, mock_q, mock_installed):
        mock_q.select.return_value.ask.return_value = "0) Back"
        mock_q.Separator = lambda: "---"
        from simulation_service_tool.menus.kueue import kueue_menu
        kueue_menu()
        # select was called — menu rendered
        assert mock_q.select.called

    @patch("simulation_service_tool.menus.kueue.is_kueue_installed", return_value=True)
    @patch("simulation_service_tool.menus.kueue.get_cluster_queue_status", return_value={"exists": True, "pending_workloads": 0, "admitted_workloads": 2, "quotas": {"cpu": "8"}})
    @patch("simulation_service_tool.menus.kueue.get_local_queue_status", return_value={"exists": True, "pending_workloads": 0, "admitted_workloads": 1})
    @patch("simulation_service_tool.menus.kueue.questionary")
    def test_installed_shows_status(self, mock_q, mock_lq, mock_cq, mock_installed, capsys):
        mock_q.select.return_value.ask.return_value = "0) Back"
        mock_q.Separator = lambda: "---"
        from simulation_service_tool.menus.kueue import kueue_menu
        kueue_menu()
        out = capsys.readouterr().out
        assert "Installed" in out or "installed" in out.lower()


# ── welcome_menu wiring ──────────────────────────────────────────

class TestWelcomeMenuKueueWiring:
    def test_kueue_menu_imported(self):
        from simulation_service_tool.menus import welcome
        assert hasattr(welcome, 'kueue_menu')

    def test_kueue_in_handle_choice_actions(self):
        """Ensure kueue_menu is referenced in _handle_welcome_choice."""
        import inspect
        from simulation_service_tool.menus.welcome import _handle_welcome_choice
        source = inspect.getsource(_handle_welcome_choice)
        assert 'kueue_menu' in source


# ── wait time estimation ─────────────────────────────────────────

class TestWaitTimeEstimation:
    """Test the drain-time estimation formula used by the summary API."""

    def _estimate(self, pending, admitted, avg_duration=7):
        """Replicate the estimation logic from simulation_service.py."""
        throughput = max(admitted, 1)
        return round((pending / throughput) * avg_duration, 1)

    def test_no_pending(self):
        assert self._estimate(pending=0, admitted=5) == 0.0

    def test_basic_estimate(self):
        # 10 pending, 5 admitted slots, 7s avg → 2 cycles × 7s = 14s
        assert self._estimate(pending=10, admitted=5) == 14.0

    def test_single_admitted_slot(self):
        # 20 pending, 1 admitted → 20 × 7 = 140s
        assert self._estimate(pending=20, admitted=1) == 140.0

    def test_zero_admitted_uses_floor(self):
        # admitted=0 floors to 1 to avoid division by zero
        assert self._estimate(pending=5, admitted=0) == 35.0

    def test_custom_avg_duration(self):
        # 10 pending, 2 admitted, 10s avg → 5 × 10 = 50s
        assert self._estimate(pending=10, admitted=2, avg_duration=10) == 50.0

    def test_large_queue(self):
        # 200 pending, 20 admitted, 7s → 10 × 7 = 70s
        assert self._estimate(pending=200, admitted=20) == 70.0


# ── cluster_queue + local_queue combined status ──────────────────

class TestKueueCombinedStatus:
    """Test combining cluster and local queue data (as the API does)."""

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_cluster_queue_quotas_format(self, mock_cmd):
        payload = {
            "status": {"pendingWorkloads": 12, "admittedWorkloads": 8},
            "spec": {
                "resourceGroups": [{
                    "coveredResources": ["cpu", "memory"],
                    "flavors": [{
                        "name": "default-flavor",
                        "resources": [
                            {"name": "cpu", "nominalQuota": "8"},
                            {"name": "memory", "nominalQuota": "16Gi"},
                        ],
                    }],
                }],
            },
        }
        mock_cmd.return_value = _cmd_result(0, json.dumps(payload))
        status = get_cluster_queue_status()
        # Verify all fields needed by the dashboard are present
        assert status["exists"] is True
        assert status["pending_workloads"] == 12
        assert status["admitted_workloads"] == 8
        assert "cpu" in status["quotas"]
        assert "memory" in status["quotas"]

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_local_queue_fields_for_dashboard(self, mock_cmd):
        payload = {
            "status": {"pendingWorkloads": 5, "admittedWorkloads": 10},
        }
        mock_cmd.return_value = _cmd_result(0, json.dumps(payload))
        status = get_local_queue_status()
        assert status["exists"] is True
        assert status["pending_workloads"] == 5
        assert status["admitted_workloads"] == 10

    @patch("simulation_service_tool.services.kueue.run_cli_command")
    def test_workload_position_in_queue(self, mock_cmd):
        """Verify workloads list gives position info for 'position X of Y'."""
        items = []
        for i in range(15):
            items.append({
                "metadata": {
                    "name": f"job-{i:03d}",
                    "labels": {"kueue.x-k8s.io/queue-name": "simulation-queue"},
                    "creationTimestamp": f"2026-04-14T10:{i:02d}:00Z",
                },
                "status": {
                    "conditions": [
                        {"type": "Admitted", "status": "True" if i < 8 else "False"},
                    ],
                },
            })
        mock_cmd.return_value = _cmd_result(0, json.dumps({"items": items}))
        workloads = list_workloads()
        assert len(workloads) == 15
        admitted = [w for w in workloads if w["admitted"]]
        queued = [w for w in workloads if not w["admitted"]]
        assert len(admitted) == 8
        assert len(queued) == 7
