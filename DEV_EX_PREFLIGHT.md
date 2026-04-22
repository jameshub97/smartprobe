# Pre-flight Pipeline

This document describes the full pre-flight check pipeline that runs before every test — from the initial port cleanup up to conflict resolution. The implementation lives in `simulation_service_tool/cli/preflight.py` and `cli/preflight_support.py`.

---

## Overview

```
preflight_check()
    └── _get_preflight(service_running)
            ├── 1. Hung API listener cleanup
            ├── 2. Service offline — fast path to direct mode
            ├── 3. TCP responsiveness probe
            ├── 4. Docker services reachability check
            ├── 5. API call → /api/preflight
            └── 6. Fallback detection → direct mode
```

If conflicts are found, `preflight_check()` calls:
- `_auto_fix_conflicts()` — silent auto-fix attempt  
- `_handle_remaining_preflight_conflicts()` — interactive loop for anything left over

---

## Stage 1 — Hung API Listener Cleanup

**Function:** `_clear_hung_api_listeners_before_preflight(service_running)`  
**Helper:** `clear_hung_api_listeners_before_preflight` in `preflight_support.py`

Before making any network call, the pipeline checks for stale OS-level listeners on the simulation service port (e.g. a previous run that exited without releasing the socket). These listeners accept TCP connections but never respond, which causes downstream probes to hang.

If a hung listener is found and cleaned up, `service_running` is downgraded to `False` so the pipeline enters direct mode instead of trying to call the (now-dead) service.

---

## Stage 2 — Service Offline Fast Path

If `service_running` is `False` after Stage 1, the pipeline immediately calls `direct_preflight_check()` and returns. No network I/O is attempted. This is the normal path when the Flask service has not been started.

---

## Stage 3 — TCP Responsiveness Probe

**Function:** `_probe_sim_api(timeout=2.5)`  
**Helper:** `probe_sim_api` in `preflight_support.py`

Even when the service is "running" (i.e. the PID is alive and the port is bound), it may be stuck — accepting TCP connections but not processing requests. This happens after an unhandled exception blocks the Flask worker thread.

The probe makes a `GET /health` request with a 2.5 s timeout. If the request times out or raises an exception, preflight falls back to direct mode rather than hanging for 10+ seconds on the real `/api/preflight` call.

```
WARN  Simulation service is not responding (API probe timed out).
INFO  Falling back to direct preflight checks.
```

---

## Stage 4 — Docker Services Reachability Check

**Function:** `_check_docker_services()`  
**Helper:** `check_docker_services` in `preflight_support.py`

Probes the two local dependencies the agents require:
- **Backend API** — port 5001
- **Simulation service** — port 5002

Checks are HTTP health-endpoint probes (`GET /health`, 2.5 s timeout) with a TCP fallback. Any service that is offline is reported before the test starts, so the user learns about the problem now rather than mid-test.

```
WARN  Service(s) unreachable before preflight: backend-api
INFO  Tests require both the simulation service (5002) and backend API (5001).
```

This is advisory — it does not stop the preflight; it surfaces information.

---

## Stage 5 — API Call to /api/preflight

**Function:** `call_service('/api/preflight')`  
**Server-side:** `K8sSimulationMonitor.preflight_check()` in `simulation_service.py`

The Flask endpoint runs three `kubectl`/`helm` checks via `run_cli_command()`:

| Check | Command | Conflict type |
|---|---|---|
| Helm releases | `helm list` filtered for `playwright-actor-*` | `helm_releases` |
| PVC exists | `kubectl get pvc playwright-cache` | `pvc` |
| PDB exists | `kubectl get pdb playwright-agent-pdb` | `pdb` |

Returns:
```json
{
  "has_conflicts": true,
  "conflicts": [
    { "type": "helm_releases", "releases": ["playwright-actor-abc123"], "fix": "helm uninstall..." },
    { "type": "pvc", "name": "playwright-cache", "fix": "kubectl delete pvc..." }
  ]
}
```

`run_cli_command()` is used (not raw `subprocess.run`) so that tools like `helm` are found via `/opt/homebrew/bin` even when the Flask process doesn't inherit the full shell PATH.

---

## Stage 6 — Fallback Detection

**Function:** `_should_fallback_to_direct(error_message)`  
**Helper:** `should_fallback_to_direct` in `preflight_support.py`

If the API call in Stage 5 returns an error, the pipeline inspects it for fallback markers:

```python
fallback_markers = ['404', 'not found', '<!doctype html', '<html', 'could not connect to simulation service']
```

Any match means the endpoint is missing from the running service (stale process, old code). The user sees an interactive prompt:

```
WARN  Preflight endpoint unavailable. Falling back to direct preflight checks.

Fallback action:
  > Continue with direct cleanup (recommended)
    Get more info
    Return to conflict menu
```

- **Continue** → calls `direct_preflight_check()`, which re-runs the same three checks locally with `kubectl`/`helm`
- **Get more info** → prints detailed explanation including the raw error, what direct mode does, and how to fix permanently (restart the service)
- **Return to conflict menu** → returns `{'cancelled': True}` so the caller can re-present the conflict menu

---

## Post-Preflight — Auto-fix

**Function:** `_auto_fix_conflicts(preflight)`

If the result has conflicts, `preflight_check()` attempts a silent auto-fix before showing anything to the user:

| Conflict type | Fix |
|---|---|
| `helm_releases` | `direct_release_cleanup(release)` for each release |
| `pvc` | `kubectl delete pvc <name> --ignore-not-found` |
| `pdb` | `kubectl delete pdb <name> --ignore-not-found` |

Returns `True` if any cleanup was attempted. Preflight is then refreshed, and if clean, the test proceeds without user interaction.

---

## Post-Preflight — Remaining Conflict Loop

**Function:** `_handle_remaining_preflight_conflicts(preflight, service_running)`

If conflicts remain after auto-fix, the user enters an interactive resolution loop:

```
WARN  Conflicts detected:
   - helm_releases: playwright-actor-abc123
     Fix: helm uninstall playwright-actor-abc123

INFO  Initialization and auto-fix already handled the standard blockers.
INFO  Use Cleanup Center or re-initialize if the cluster still drifted.

  > Open Cleanup Center
    Re-initialize Cluster
    Refresh preflight status
    Start anyway (may fail)
    Cancel
```

The loop continues until either:
- The user chooses **Cancel** → returns `False`, test is aborted
- The user chooses **Start anyway** → returns `True`, test proceeds with conflicts
- A **Refresh** confirms conflicts are cleared → returns `True`

---

## Post-Start Error Recovery

**Function:** `_handle_start_error_recovery(error_message, service_running)`

If the test start itself fails (e.g. Helm errors during deployment), the error message is parsed for a conflicting release name via regex (`current value is "([^"]+)"`). The user is offered:

```
  > Delete conflicting release 'playwright-actor-abc123'
    Delete shared stuck resources (PVCs / PDBs)
    Refresh cluster status
    Return to menu
```

- **Delete conflicting release** — validates the name against `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$` before calling `direct_release_cleanup()`
- **Delete shared stuck resources** — calls `direct_stuck_cleanup()`
- **Refresh cluster status** — calls `direct_verify_state()` and renders the status summary

---

## Module Map

| File | Role |
|---|---|
| `cli/preflight.py` | Orchestration: pipeline, prompts, conflict loops |
| `cli/preflight_support.py` | Pure helpers: probes, fallback detection, info text |
| `services/api_client.py` | `call_service()` — HTTP calls to Flask |
| `services/direct_cleanup.py` | `direct_preflight_check()`, `direct_release_cleanup()`, etc. |
| `services/hung_api_cleanup.py` | `clear_hung_api_listeners()` |
| `simulation_service.py` | `K8sSimulationMonitor.preflight_check()` — server-side checks |
