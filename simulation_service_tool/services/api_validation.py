"""Endpoint-level API validation for all platform services.

Probes each service beyond a single /health ping:
  - measures response time
  - validates response schema
  - distinguishes offline from degraded
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Endpoint registry
# ---------------------------------------------------------------------------

ENDPOINTS: list[dict] = [
    {
        "service": "Simulation service",
        "method": "GET",
        "url": "http://localhost:5002/health",
        "expect_status": 200,
    },
    {
        "service": "Simulation service",
        "method": "GET",
        "url": "http://localhost:5002/api/simulation/summary",
        "expect_status": 200,
        "expect_keys": ["total", "running"],
    },
    {
        "service": "Backend API",
        "method": "GET",
        "url": "http://localhost:5001/health",
        "expect_status": 200,
    },
    {
        "service": "Backend API",
        "method": "GET",
        "url": "http://localhost:5001/api/assets",
        "expect_status": [200, 401],  # 401 counts as reachable
    },
    {
        "service": "Frontend",
        "method": "GET",
        "url": "http://localhost:3000/",
        "expect_status": 200,
    },
]

_TIMEOUT = 3.0


# ---------------------------------------------------------------------------
# Core probe
# ---------------------------------------------------------------------------

def probe_endpoint(endpoint: dict) -> dict:
    """Probe a single endpoint.

    Returns::

        {
          'service': str,
          'url': str,
          'status': int | None,
          'ok': bool,
          'latency_ms': float,
          'error': str | None,       # set when ok is False
          'schema_ok': bool | None,  # None when not checked
        }
    """
    url = endpoint["url"]
    expect = endpoint.get("expect_status", 200)
    if isinstance(expect, int):
        expect = [expect]
    expect_keys: list[str] = endpoint.get("expect_keys", [])

    start = time.monotonic()
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            latency_ms = (time.monotonic() - start) * 1000
            status = resp.status
            body = resp.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        latency_ms = (time.monotonic() - start) * 1000
        status = exc.code
        body = ""
    except OSError:
        latency_ms = (time.monotonic() - start) * 1000
        return {
            "service": endpoint["service"],
            "url": url,
            "status": None,
            "ok": False,
            "latency_ms": round(latency_ms),
            "error": "connection refused",
            "schema_ok": None,
        }
    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000
        return {
            "service": endpoint["service"],
            "url": url,
            "status": None,
            "ok": False,
            "latency_ms": round(latency_ms),
            "error": str(exc)[:80],
            "schema_ok": None,
        }

    ok = status in expect

    schema_ok: bool | None = None
    if ok and expect_keys:
        try:
            import json
            data = json.loads(body)
            schema_ok = all(k in data for k in expect_keys)
            if not schema_ok:
                missing = [k for k in expect_keys if k not in data]
                return {
                    "service": endpoint["service"],
                    "url": url,
                    "status": status,
                    "ok": False,
                    "latency_ms": round(latency_ms),
                    "error": f"missing keys: {', '.join(missing)}",
                    "schema_ok": False,
                }
        except Exception:
            schema_ok = False

    return {
        "service": endpoint["service"],
        "url": url,
        "status": status,
        "ok": ok,
        "latency_ms": round(latency_ms),
        "error": None if ok else f"HTTP {status}",
        "schema_ok": schema_ok,
    }


# ---------------------------------------------------------------------------
# Multi-endpoint probe (sequential — avoids subprocess overhead)
# ---------------------------------------------------------------------------

def validate_all(endpoints: list[dict] | None = None) -> list[dict]:
    """Probe all registered endpoints and return results."""
    return [probe_endpoint(ep) for ep in (endpoints or ENDPOINTS)]


def validate_service(service_name: str) -> list[dict]:
    """Probe only the endpoints for a given service name."""
    return [
        probe_endpoint(ep)
        for ep in ENDPOINTS
        if ep["service"] == service_name
    ]


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def summarise(results: list[dict]) -> dict:
    """Aggregate results into a top-level summary.

    Returns::

        {
          'total': int,
          'ok': int,
          'degraded': int,
          'offline': int,
          'by_service': {service_name: {'ok': int, 'total': int}},
        }
    """
    ok = sum(1 for r in results if r["ok"])
    offline = sum(1 for r in results if r["status"] is None)
    degraded = len(results) - ok - offline

    by_service: dict[str, dict] = {}
    for r in results:
        svc = r["service"]
        bucket = by_service.setdefault(svc, {"ok": 0, "total": 0})
        bucket["total"] += 1
        if r["ok"]:
            bucket["ok"] += 1

    return {
        "total": len(results),
        "ok": ok,
        "degraded": degraded,
        "offline": offline,
        "by_service": by_service,
    }
