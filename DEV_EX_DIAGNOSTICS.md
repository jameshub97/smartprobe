# Dev Experience: Pre-flight Diagnostics Roadmap

Captures patterns discovered during debugging sessions that should be automated into the CLI and simulation service to eliminate repeated manual investigation.

---

## 1. Image Readiness Check

**Problem discovered:** `ErrImageNeverPull` and `ErrImagePull` when pods start — image not present in cluster nodes' containerd, or local registry not trusted.

**Current workaround:** Manual `kind load docker-image` or DaemonSet patch.

### Proposed: `check images` CLI command

Run before `Start a Test`. Surface the full picture in one pass:

```
[Image Readiness]
  ✓ Docker daemon reachable
  ✓ playwright-agent:latest present in Docker (sha: 42154f5b)
  ✓ local-registry running on :5050
  ✓ playwright-agent:latest pushed to localhost:5050
  ? Cluster type: docker-desktop (10 nodes via VM, NOT Docker containers)
  ✗ Nodes require insecure registry trust for host.docker.internal:5050
      → Fix: deploy registry-config-patch DaemonSet (see below)
  ✓ alpine:3.18 pullable (used by patch DaemonSet)
```

#### Checks to implement

| Check | Method | Pass condition |
|---|---|---|
| Docker daemon live | `docker info` / socket ping | exit 0 |
| Image exists locally | `docker images <repo>:<tag> --format json` | non-empty |
| Local registry running | `curl -s http://localhost:5050/v2/_catalog` | 200 + repo listed |
| Image pushed to registry | same catalog endpoint | repo name present |
| Cluster type | `kubectl get nodes -o json` → `containerRuntime` field | detect kind / docker-desktop / minikube |
| Kind nodes have image | `docker exec kind-control-plane ctr -n k8s.io images ls` | image digest present |
| DD nodes trust registry | `kubectl exec -n kube-system ds/registry-config-patch` init log | "Done" or "Already patched" |

---

## 2. Registry Trust DaemonSet (Automated Patch)

**Problem:** Docker Desktop k8s nodes live in VMs. Containerd on those nodes routes pulls through an internal mirror that blocks unknown HTTP registries. `pullPolicy: Never` breaks if the image was never imported to that node; `pullPolicy: IfNotPresent` fails if the node hasn't been configured to trust `host.docker.internal:5050`.

**Solution found:** Deploy a privileged `initContainer` DaemonSet that appends to `/etc/containerd/config.toml` and sends `SIGHUP` to containerd on every node.

```toml
[plugins."io.containerd.grpc.v1.cri".registry.configs."host.docker.internal:5050".tls]
  insecure_skip_verify = true
```

### Productise as a CLI action

- Add **"Setup Cluster"** to the welcome menu (runs once, idempotent)
- DaemonSet checks for existing config before patching (`grep -q MARKER`)
- After apply, poll until all init containers complete, then report per-node status
- Clean up the DaemonSet after all nodes succeed (pause container not needed long-term)
- Store a `~/.smartprobe/cluster-setup-done` marker to skip on subsequent runs

---

## 3. Release Conflict Check (Matching Releases)

**Problem:** `helm list --short` wasn't run at all before deploy → leftover releases from crashed tests cause new deploy to fail or silently clash.

**Current state:** `/api/preflight` checks for PVCs, PDBs, and existing helm releases but `helm` was called via raw `subprocess.run` (no PATH), causing a 500.

### Proposed: Match releases to active tests

When a test starts, record `{release_name: {started_at, mode, agents, status}}` in the in-memory state. On preflight:

1. `helm list -o json` → all releases
2. Cross-reference with `simulation_state` — flag releases with no active sim entry as **orphaned**
3. Cross-reference with k8s jobs — flag releases whose Job is `Complete`/`Failed` but TTL hasn't cleaned up
4. Show table in CLI:

```
[Release Conflicts]
  small-1776640981   orphaned    started 4h ago    → helm uninstall small-1776640981
  small-1776642518   complete    finished 10m ago  → cleaning up (TTL 55m remaining)
```

Auto-offer one-key fix: `[C]lean all orphaned` before proceeding.

---

## 4. Flask Service Health Checks

**Problem:** Multiple instances: simulation service (5002), coordinator (5003), backend (5001). When a test fails it's not obvious which one is down. CLI calls `/api/preflight` which calls `K8sSimulationMonitor.preflight_check()` which calls `helm list` — a chain of 3 dependencies, any of which can be `FileNotFoundError`.

### Proposed: `probe services` command + startup health banner

#### On CLI startup, print:

```
[Service Health]
  ✓ Simulation service  http://localhost:5002  (v0.9.1, uptime 2h)
  ✓ Coordinator         http://localhost:5003  (FastAPI)
  ✗ Backend             http://localhost:5001  → not reachable
      Start: cd asset-manager-1/backend && dotnet run

[Tooling]
  ✓ kubectl   /opt/homebrew/bin/kubectl   v1.34.3
  ✓ helm      /opt/homebrew/bin/helm      v3.17.0
  ✓ docker    /usr/local/bin/docker       27.5.1
  ✗ kind      not found  (required for image loading)
```

#### Checks to implement

| Service | Check | Endpoint |
|---|---|---|
| Simulation Flask | `GET /health` | `localhost:5002` |
| Coordinator | `GET /health` | `localhost:5003` |
| Backend (C#) | `GET /api/health` | `localhost:5001` |
| Binary: kubectl | `which kubectl` + `kubectl version --client` | — |
| Binary: helm | `which helm` + `helm version --short` | — |
| Binary: docker | `docker info` | — |
| Binary: kind | `which kind` + `kind version` | — |

**Fix for `FileNotFoundError`:** All subprocess calls in `simulation_service.py` should go through `run_cli_command()` which catches `FileNotFoundError` and returns `returncode=127`. Done for `preflight_check` — apply same pattern everywhere.

---

## 5. Playwright Agent Image Pipeline Check

**Problem:** The agent image (`playwright-agent:latest`) must exist in Docker, be pushed to the local registry, and be pullable from cluster nodes — three separate states that drift independently.

### Check sequence

```
[Agent Image Pipeline]
  1. Docker local:   playwright-agent:latest   sha:42154f5b  ✓
  2. Registry:       localhost:5050/playwright-agent:latest  ✓  (pushed 2m ago)
  3. Pull test:      host.docker.internal:5050/playwright-agent:latest
                     from pod on desktop-worker3             ✓  (200ms)
  4. run.py present: docker run --rm playwright-agent:latest
                     python3 -c "import run" (dry check)     ✓
```

If step 3 fails → offer to run the containerd patch DaemonSet (item 2 above).
If step 2 fails → offer `docker push localhost:5050/playwright-agent:latest`.
If step 1 fails → offer `docker build -t playwright-agent:latest .` (with Dockerfile path).

---

## 6. Cluster Type Detection Strategy

Discovered three co-existing clusters with different image loading requirements:

| Cluster | Context | Image Strategy |
|---|---|---|
| Docker Desktop k8s (10 nodes) | `docker-desktop` | Push to local registry + patch containerd insecure trust |
| Kind | `kind-kind` | `kind load docker-image` OR local registry via `kind create cluster --config` with registry mirror |
| Minikube | `minikube` | `minikube image load` or `eval $(minikube docker-env)` then build |

Detection logic (add to CLI `setup_cluster`):

```python
def detect_cluster_type(context_name: str) -> str:
    if context_name == 'docker-desktop':
        return 'docker-desktop'
    if context_name.startswith('kind-'):
        return 'kind'
    if context_name == 'minikube':
        return 'minikube'
    return 'generic'
```

Route to correct image-loading strategy automatically.

---

## 7. Quick-action Order (Suggested CLI "Setup" Flow)

```
smartprobe setup

  [1/5] Checking tooling (kubectl, helm, docker, kind)...     ✓
  [2/5] Checking Docker images...                             ✓ playwright-agent:latest present
  [3/5] Checking local registry (:5050)...                    ✓ running, image pushed
  [4/5] Patching cluster nodes (insecure registry trust)...   ✓ 10/10 nodes patched
  [5/5] Checking service health (5001, 5002, 5003)...         ✗ backend not reachable

  Setup complete with 1 warning.
  → Start backend: cd asset-manager-1/backend && dotnet run
```

All steps idempotent — safe to re-run any time something looks broken.
