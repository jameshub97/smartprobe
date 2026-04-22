# Smartprobe: Current Developer Experience

A ground-level description of what it actually takes to run a simulation test today — every step, every tool, and every manual action in the loop.

---

## Overview

Smartprobe is a load-testing platform that spins up fleets of Playwright agents inside Kubernetes, drives them against a C# backend, and surfaces real-time results in a Flask-backed dashboard. The developer experience is delivered through:

- An **interactive CLI** (`simulation_service_tool`) — the primary interface for all test operations
- A **simulation service** (Flask, `:5002`) — orchestrates Helm deployments and exposes the backend API
- A **coordinator service** (FastAPI, `:5003`) — tracks agent activity in real time
- A **dashboard** (`/dashboard/index.html`) — live view of pod states, stats, and activity logs
- A **C# backend** (ASP.NET, `:5001`) — the system under test

---

## Boot Sequence

When you run `python3 -m simulation_service_tool` the CLI runs four things automatically before showing a menu:

### 1. Kubernetes gate (`_early_k8s_check`)
Pings the Kubernetes API with a short timeout. If it's unreachable, you land in a **recovery loop** that offers:
- Restart / start an existing Kind container
- Delete and recreate the Kind cluster
- Start a minikube cluster
- Show diagnostic details
- Re-check / exit

This is a hard prerequisite — nothing proceeds if k8s can't be reached.

### 2. Simulation service probe (`_early_api_check`)
Hits `http://localhost:5002/health`. If offline, prompts:
- Start the service now (spawns `python3 simulation_service.py server`)
- Continue anyway

### 3. Parallel startup diagnostics (`_run_startup_diagnostics`)
Seven checks run concurrently and print a one-line status table:

| Check | Target |
|---|---|
| Simulation service | `http://localhost:5002/health` |
| Backend API | `http://localhost:5001/health` |
| Transfer Stacker | `http://localhost:5173/` |
| PostgreSQL | `localhost:5432` TCP |
| Docker API | Unix socket `/var/run/docker.sock` |
| Cluster runtime | kubectl + context inspection |
| Kubernetes API | kubectl version probe |

Docker unreachable is a hard exit. Everything else is a warning.

### 4. Welcome menu
Selects one of four menu variants based on:
- `service_running` — can the Flask API respond?
- `cluster_initialized` — have Helm RBAC/namespace charts been applied?
- `drift_findings` — are there orphaned releases, PVCs, PDBs, or unhealthy pods?

---

## Welcome Menu Variants

| State | Menu shown | First option |
|---|---|---|
| Not initialized + cluster dirty | Menu A | Initialize Cluster |
| Not initialized + cluster clean | Menu B | Start a Test |
| Initialized + drift detected | Menu C | Fix All Drift Issues (with banner) |
| Initialized + no drift | Menu D | Start a Test |

All four variants always include: Kill All Pods (K) and Exit.

---

## Starting a Test

Path: **Start a Test → mode → preset or custom → confirm → deploy**

### Mode selection
- `basic` — standard Playwright e2e (agents run `python3 /app/run.py`)
- `transactional` — browse and transfer assets; agents connect to the C# backend + coordinator

### Presets

| Name | Agents | Parallel | Est. time | Purpose |
|---|---|---|---|---|
| tiny | 5 | 2 | ~10s | Quick smoke check |
| small | 10 | 5 | ~30s | Dev iteration |
| medium | 50 | 10 | ~2m | Integration run |
| large | 100 | 20 | ~5m | Performance baseline |
| xlarge | 500 | 50 | ~15m | Stress test |
| throughput | 500 | 100 | varies | High-frequency, low-resource |

### Custom configuration
When not using a preset, the CLI prompts for: completions, parallelism, persona (`impatient` / `strategic` / `browser`), memory/CPU requests and limits, backoff limit, TTL, and whether to enable Kueue queuing.

### Pre-flight conflict check
Before deploying, the CLI calls `GET /api/preflight` on the simulation service. This checks for:
- Existing `playwright-cache` PVC
- Legacy `playwright-agent-pdb` PodDisruptionBudget
- Any existing Helm releases from a previous run

If conflicts are found, the CLI offers auto-fix or manual cleanup before proceeding.

### Helm deploy
The simulation service runs `helm install` using the chart at `helm/playwright-agent/`. Key values set at deploy time:
- `image.repository` — `host.docker.internal:5050/playwright-agent` (local registry)
- `image.pullPolicy` — `IfNotPresent`
- `commandOverride` — `python3 /app/run.py`
- `ttlSecondsAfterFinished` — `3600` (pods stay visible for 1 hour post-completion)
- `statefulset.enabled` — `false` (only a Job is created; prevents crash loop)
- `targetUrl`, `simApi`, `backendApi`, `coordApi` — all via `host.docker.internal` for in-cluster → host routing

---

## Image Dependency (Current Manual Step)

The agent image (`playwright-agent:latest`) must exist in Docker and be reachable from cluster nodes before pods can start.

**Current state:**  
Nodes pull from `host.docker.internal:5050` (a local `registry:2` container on port 5050). Node containerd must trust this HTTP registry. This is handled by the `registry-config-patch` DaemonSet which patches `/etc/containerd/config.toml` on every node and sends `SIGHUP` to containerd.

**What still requires manual action:**
1. Build or tag the agent image: `docker tag playwright-agent:latest localhost:5050/playwright-agent:latest`
2. Push to registry: `docker push localhost:5050/playwright-agent:latest`
3. Deploy the DaemonSet patch if nodes haven't been configured yet

If this hasn't been done, pods start with `ErrImageNeverPull` (pullPolicy=Never) or `ErrImagePull` (registry not trusted).

---

## Cluster Type Context

Three clusters co-exist on this machine with different image delivery requirements:

| Context | Type | Image strategy |
|---|---|---|
| `docker-desktop` | Docker Desktop k8s (10 VM-based nodes) | Local registry + containerd trust patch |
| `kind-kind` | Kind (Docker container node) | `kind load docker-image` or local registry |
| `minikube` | Minikube | `minikube image load` |

The default active context is `docker-desktop`. Switching context requires `kubectl config use-context`.

---

## Watching a Test

After a test starts, the "Watch Progress" option tails pod state via `kubectl get pods -w`. The CLI shows a live table of pod name, ready state, status, restart count, and age, filtered by the release label.

Press Ctrl+C to return to the main menu at any time.

---

## Agent Logs

**Agent Logs** (from the welcome menu) shows a pod picker sorted by problem severity (erroring pods first), then displays the last 100 log lines. Optionally streams live via `kubectl logs -f`.

---

## Drift Detection (Background)

Every time the welcome menu loops (when cluster is initialized), `run_drift_checks()` runs a sequential probe pipeline:

1. Docker daemon reachable?
2. Kubernetes API reachable?
3. Orphaned Helm releases / PVCs / PDBs?
4. Stale non-Running or not-ready pods?
5. Simulation service offline?
6. Docker Compose stack health (informational)

If any drift is found, the welcome screen shows a numbered banner and promotes "Fix All Drift Issues" to menu option 1. "Fix All" calls `remediate_all()` which auto-runs `direct_quick_cleanup()` for resource drift and `_restart_service()` for the offline service check. K8s unreachability is intentionally not auto-fixed — it routes to Diagnostics → K8s Connectivity.

---

## Sub-menus

| Menu | Purpose |
|---|---|
| **Cleanup Center** | Quick clean, full reset, stuck resources, release-specific cleanup, verify state |
| **Diagnostics** | Parallel quick-probe panel (service + k8s + compose + endpoints + drift), K8s connectivity, recover |
| **Routine Checks** | Unhealthy pod summary, preflight conflicts, stale StatefulSet inspection, port status |
| **Monitoring** | Install/uninstall Prometheus+Grafana stack, apply ServiceMonitor, view Grafana access URL |
| **Kueue** | Install/uninstall Kueue, apply ClusterQueue + LocalQueue, list workloads |
| **Docker Compose** | Start/stop the full docker-compose stack, view per-service health |
| **Start Service** | Launch `simulation_service.py server` in a subprocess |
| **Kill All Pods** | Nuke everything (all resources) or pods-only |

---

## Service Ports Reference

| Port | Service | Technology |
|---|---|---|
| 5001 | Backend (system under test) | ASP.NET Core |
| 5002 | Simulation service | Flask (Python) |
| 5003 | Coordinator service | FastAPI (Python / uvicorn) |
| 5050 | Local image registry | Docker `registry:2` |
| 5173 | Transfer Stacker frontend | Vite / Vue |
| 5432 | PostgreSQL | Postgres |

---

## Typical Dev Iteration Loop

```
1. Confirm services are up
   → CLI boot diagnostics + _early_api_check

2. If image has changed:
   → docker build -t playwright-agent:latest .
   → docker push localhost:5050/playwright-agent:latest

3. Start test
   → Start a Test → basic → small → confirm
   → CLI calls /api/preflight → runs helm install
   
4. Watch pods
   → Watch Progress (kubectl watch filtered by release)
   → or: Agent Logs for per-pod log inspection

5. If something's wrong:
   → Diagnostics menu → quick probe panel
   → Routine Checks → unhealthy pod diagnosis
   → Cleanup Center → quick clean

6. After test:
   → Pods remain for 1 hour (ttlSecondsAfterFinished=3600)
   → Results visible in dashboard at localhost:5002/
   → Cleanup Center → quick clean when done
```

---

## Known Manual Steps (Gaps in Automation)

These are currently not automated and must be done by hand:

| Step | Command |
|---|---|
| Build agent image | `docker build -t playwright-agent:latest .` |
| Push to local registry | `docker push localhost:5050/playwright-agent:latest` |
| Patch containerd on nodes | Deploy `registry-config-patch` DaemonSet (or wait for first-time setup) |
| Switch k8s context | `kubectl config use-context <name>` |
| Start backend | `cd asset-manager-1/backend && dotnet run` |
| Start coordinator | `uvicorn coordinator_service:app --port 5003` |
| Start Vue frontend | `cd vue-project && npm run dev` |

These gaps are tracked in [DEV_EX_DIAGNOSTICS.md](DEV_EX_DIAGNOSTICS.md) as candidates for the `smartprobe setup` automated flow.
