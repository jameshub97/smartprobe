# Simulation Service

Flask HTTP server that orchestrates distributed agent test runs on Kubernetes via Helm, exposes a live dashboard, and streams real-time results to the browser.

**Port:** `5002`  
**Entry point:** `simulation_service.py`  
**Dashboard:** `dashboard/index.html` (served at `/`)

---

## Starting the server

```bash
cd smartprobe
python3 simulation_service.py server
# or with options:
python3 simulation_service.py server --host 0.0.0.0 --port 5002 --debug
```

Running with no arguments launches the interactive CLI tool instead.

---

## Authentication

Mutating endpoints (`/api/simulation/start`, `/api/simulation/stop`, and all `/api/cleanup/*`) require a Bearer token:

```
Authorization: Bearer <api-key>
```

The key is read from the environment variable `SIMULATION_API_KEY`.  
Default (development only): `dev-key-change-in-production`.

---

## API Reference

### Health

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | — | Returns `{"status":"ok","time":"..."}` |
| `GET` | `/metrics` | — | Prometheus scrape endpoint |

---

### Simulation — read

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/simulation/summary` | — | Full k8s pod summary + coordinator stats + Prometheus gauges. Cached per TTL. |
| `GET` | `/api/simulation/activity` | — | Recent activity log (last N events) + cumulative `totals`. |
| `GET` | `/api/simulation/agent-states` | — | Latest in-memory state per pod (keyed by pod name). |
| `GET` | `/api/simulation/live-logs` | — | Recent stdout from all currently Running pods. Query: `?tail=8` (max 50). |
| `GET` | `/api/simulation/agent-results` | — | Last 200 agent final-result payloads. |
| `GET` | `/api/simulation/agent-detail/<pod>` | — | State + result + coordinator role for one pod. |
| `GET` | `/api/simulation/pod-logs/<pod>` | — | `kubectl logs` for a specific pod. Query: `?tail=300` (max 1000). |
| `GET` | `/api/simulation/tests` | — | `helm list` of active test releases. |
| `GET` | `/api/simulation/presets` | — | Returns the built-in preset definitions. |
| `GET` | `/api/simulation/status` | — | Lightweight pod count via kubectl. |

---

### Simulation — control

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/simulation/start` | ✓ | Launch a test. See [Start payload](#start-payload) below. |
| `POST` | `/api/simulation/stop` | ✓ | `helm uninstall` a named release. Body: `{"name":"<release>"}` |

#### Start payload

```json
{
  "name": "my-run-1234",
  "completions": 10,
  "parallelism": 5,
  "persona": "impatient",
  "mode": "basic",
  "imageRepository": "playwright-agent",
  "imageTag": "latest",
  "commandOverride": "python3 /app/run.py",
  "ttlSecondsAfterFinished": 3600,
  "requestMemory": "64Mi",
  "requestCpu": "50m",
  "limitMemory": "128Mi",
  "limitCpu": "100m",
  "backoffLimit": 2,
  "kueue": false,
  "skip_preflight": false
}
```

All fields are optional — defaults are applied automatically.  
`mode` must be `"basic"` or `"transactional"`.  
When not supplied, `imageRepository` defaults to `playwright-agent`, `commandOverride` defaults to `python3 /app/run.py`, and `ttlSecondsAfterFinished` defaults to `3600`.

---

### Agent reporting  *(called by pods during execution)*

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/simulation/agent-action` | — | Agent reports a real-time event. See [Action payload](#action-payload). |
| `POST` | `/api/simulation/agent-result` | — | Agent posts its final JSON result on completion. |

#### Action payload

```json
{
  "pod": "small-12345-agent-xk9ab",
  "action": "transfer_completed",
  "details": "asset-007 transferred to user-b"
}
```

Valid `action` values: `browsing`, `registered`, `logged_in`, `asset_created`, `asset_listed`, `transfer_started`, `transfer_completed`, `transfer_failed`, `conflict_detected`, `consistency_check`, `agent_done`.

---

### Cleanup

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/cleanup/all` | ✓ | Remove all test resources. |
| `DELETE` | `/api/cleanup/release/<name>` | ✓ | Uninstall a specific Helm release. |
| `POST` | `/api/cleanup/stuck` | ✓ | Force-delete stuck pods/jobs. |
| `POST` | `/api/cleanup/reset` | ✓ | Hard-reset the cluster to a clean state. |
| `GET` | `/api/cleanup/verify` | — | Check whether the cluster is clean. |
| `GET` | `/api/cleanup/preflight` | — | Check for resource conflicts before a deploy. |
| `GET` | `/api/preflight` | — | Alias for preflight check. |

---

### Diagnostics

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/diagnostics/deployment/<release>` | — | Image pull errors, scheduling failures. |
| `GET` | `/api/diagnostics/performance/<release>` | — | Throughput and latency analysis. |
| `GET` | `/api/diagnostics/network/<release>` | — | Network connectivity issues. |
| `GET` | `/api/diagnostics/progress/<release>` | — | Completion rate and ETA. |
| `GET` | `/api/diagnostics/cluster` | — | Full cluster health summary. |
| `GET` | `/api/diagnostics/cost/<release>` | — | Estimated resource cost. |

---

## Presets

| Preset | Agents | Parallelism | Estimated time |
|--------|--------|-------------|----------------|
| `tiny` | 5 | 2 | ~10 s |
| `small` | 10 | 5 | ~30 s |
| `medium` | 100 | 20 | ~2 min |
| `large` | 500 | 50 | ~5 min |
| `xlarge` | 2000 | 100 | ~15 min |

---

## In-memory state

State is held in process memory and resets on restart.

| Variable | Purpose |
|----------|---------|
| `_activity_log` | Ring buffer of agent events (max 500). Polled by the dashboard activity feed. |
| `_event_totals` | Cumulative counts per event type. Used as fallback when k8s pod gauges reset to 0 after cleanup. |
| `_agent_states` | Latest reported state per pod name. Never trimmed — used by the Agents tab. |
| `_agent_results` | Final result payloads from completed pods (max 200). |
| `_cache` | Short-lived cache for `get_detailed_summary()` to avoid hammering the k8s API on each 2 s dashboard poll. |

---

## Prometheus metrics

| Metric | Type | Description |
|--------|------|-------------|
| `agent_pods_active` | Gauge | Currently active pods |
| `agent_pods_succeeded` | Gauge | Succeeded pods (live from k8s) |
| `agent_pods_failed` | Gauge | Failed pods (live from k8s) |
| `agent_pods_pending` | Gauge | Pending pods (live from k8s) |
| `agent_orchestration_total` | Counter | Start/stop events labelled by result |
| `agent_test_duration_seconds` | Histogram | Per-preset test run duration |
| `simulation_http_requests_total` | Counter | HTTP requests labelled by method/endpoint/status |
| `simulation_http_request_duration_seconds` | Histogram | Request latency |

Scraped at `GET /metrics`.

---

## Related services

| Service | Port | File |
|---------|------|------|
| Coordinator | 5003 | `coordinator_service.py` |
| C# Backend (asset API) | 5001 | `transfer-stacker/backend/` |
| Vue frontend | 5173 | `transfer-stacker/vue-project/` |

The simulation service proxies coordinator stats on every `/api/simulation/summary` call (best-effort, silently ignored if port 5003 is not running).
