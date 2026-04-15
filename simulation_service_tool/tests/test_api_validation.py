"""Tests for the API validation service."""

from unittest.mock import patch, MagicMock
import io
import json
import urllib.error

import pytest

from simulation_service_tool.services.api_validation import (
    probe_endpoint,
    validate_all,
    validate_service,
    summarise,
    ENDPOINTS,
)


def _make_response(status=200, body=b"{}"):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ── probe_endpoint ─────────────────────────────────────────────────

class TestProbeEndpoint:
    def _ep(self, **overrides):
        base = {"service": "Test", "method": "GET", "url": "http://localhost:9999/test", "expect_status": 200}
        base.update(overrides)
        return base

    @patch("urllib.request.urlopen")
    def test_ok_status(self, mock_open):
        mock_open.return_value = _make_response(200, b"{}")
        result = probe_endpoint(self._ep())
        assert result["ok"] is True
        assert result["status"] == 200
        assert result["error"] is None
        assert result["latency_ms"] >= 0

    @patch("urllib.request.urlopen")
    def test_error_status(self, mock_open):
        exc = urllib.error.HTTPError(url="http://localhost:9999/test", code=503, msg="service unavailable", hdrs=None, fp=None)
        mock_open.side_effect = exc
        result = probe_endpoint(self._ep())
        assert result["ok"] is False
        assert result["status"] == 503
        assert "503" in result["error"]

    @patch("urllib.request.urlopen")
    def test_connection_refused(self, mock_open):
        mock_open.side_effect = OSError("connection refused")
        result = probe_endpoint(self._ep())
        assert result["ok"] is False
        assert result["status"] is None
        assert "connection refused" in result["error"]

    @patch("urllib.request.urlopen")
    def test_multiple_expected_status(self, mock_open):
        """401 counts as reachable when listed in expect_status."""
        mock_open.side_effect = urllib.error.HTTPError(
            url="http://localhost:9999/test", code=401, msg="Unauthorized", hdrs=None, fp=None
        )
        result = probe_endpoint(self._ep(expect_status=[200, 401]))
        assert result["ok"] is True

    @patch("urllib.request.urlopen")
    def test_schema_ok(self, mock_open):
        body = json.dumps({"total": 0, "running": 0}).encode()
        mock_open.return_value = _make_response(200, body)
        result = probe_endpoint(self._ep(expect_keys=["total", "running"]))
        assert result["ok"] is True
        assert result["schema_ok"] is True

    @patch("urllib.request.urlopen")
    def test_schema_missing_key(self, mock_open):
        body = json.dumps({"total": 0}).encode()  # "running" key missing
        mock_open.return_value = _make_response(200, body)
        result = probe_endpoint(self._ep(expect_keys=["total", "running"]))
        assert result["ok"] is False
        assert result["schema_ok"] is False
        assert "running" in result["error"]

    @patch("urllib.request.urlopen")
    def test_schema_not_checked_when_no_expect_keys(self, mock_open):
        mock_open.return_value = _make_response(200, b"OK")
        result = probe_endpoint(self._ep())
        assert result["schema_ok"] is None

    @patch("urllib.request.urlopen")
    def test_unexpected_exception(self, mock_open):
        mock_open.side_effect = Exception("unexpected error")
        result = probe_endpoint(self._ep())
        assert result["ok"] is False
        assert result["status"] is None


# ── validate_all ───────────────────────────────────────────────────

class TestValidateAll:
    @patch("simulation_service_tool.services.api_validation.probe_endpoint")
    def test_returns_result_per_endpoint(self, mock_probe):
        mock_probe.side_effect = lambda ep: {"service": ep["service"], "ok": True, "url": ep["url"], "status": 200, "latency_ms": 5, "error": None, "schema_ok": None}
        results = validate_all()
        assert len(results) == len(ENDPOINTS)

    @patch("simulation_service_tool.services.api_validation.probe_endpoint")
    def test_custom_endpoint_list(self, mock_probe):
        custom = [{"service": "X", "url": "http://x/y", "expect_status": 200}]
        mock_probe.return_value = {"service": "X", "ok": True, "url": "http://x/y", "status": 200, "latency_ms": 1, "error": None, "schema_ok": None}
        results = validate_all(custom)
        assert len(results) == 1
        mock_probe.assert_called_once_with(custom[0])


# ── validate_service ───────────────────────────────────────────────

class TestValidateService:
    @patch("simulation_service_tool.services.api_validation.probe_endpoint")
    def test_filters_by_service(self, mock_probe):
        mock_probe.side_effect = lambda ep: {"service": ep["service"], "ok": True, "url": ep["url"], "status": 200, "latency_ms": 2, "error": None, "schema_ok": None}
        results = validate_service("Simulation service")
        sim_eps = [ep for ep in ENDPOINTS if ep["service"] == "Simulation service"]
        assert len(results) == len(sim_eps)

    @patch("simulation_service_tool.services.api_validation.probe_endpoint")
    def test_unknown_service_returns_empty(self, mock_probe):
        results = validate_service("Nonexistent service")
        assert results == []
        mock_probe.assert_not_called()


# ── summarise ─────────────────────────────────────────────────────

class TestSummarise:
    def _r(self, service, ok, status=200):
        return {"service": service, "ok": ok, "status": status, "url": "", "latency_ms": 10, "error": None, "schema_ok": None}

    def test_all_ok(self):
        results = [self._r("A", True), self._r("A", True), self._r("B", True)]
        s = summarise(results)
        assert s["total"] == 3
        assert s["ok"] == 3
        assert s["degraded"] == 0
        assert s["offline"] == 0
        assert s["by_service"]["A"]["ok"] == 2
        assert s["by_service"]["B"]["ok"] == 1

    def test_mixed(self):
        results = [
            self._r("A", True),
            self._r("A", False, status=503),  # degraded (has status code)
            self._r("B", False, status=None),  # offline
        ]
        s = summarise(results)
        assert s["ok"] == 1
        assert s["degraded"] == 1
        assert s["offline"] == 1

    def test_all_offline(self):
        results = [self._r("A", False, status=None), self._r("B", False, status=None)]
        s = summarise(results)
        assert s["ok"] == 0
        assert s["offline"] == 2
        assert s["degraded"] == 0

    def test_empty(self):
        s = summarise([])
        assert s["total"] == 0
        assert s["ok"] == 0
        assert s["by_service"] == {}


# ── diagnostics menu wiring ────────────────────────────────────────

class TestDiagnosticsApiValidationWiring:
    def test_api_validation_imported(self):
        from simulation_service_tool.services import api_validation
        assert hasattr(api_validation, "validate_all")
        assert hasattr(api_validation, "summarise")
