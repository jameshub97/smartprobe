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

## 7. Docker Compose Pre-launch Checks

**Problem discovered:** `docker compose up --build` fails silently or with a cryptic `lstat ... no such file or directory` because build `context` paths in `docker-compose.yml` are wrong relative to the working directory. Currently the compose file assumed a `backend/` and `frontend/` folder inside the smartprobe root — both live in a sibling repo (`asset-manager-1/`). No pre-flight check caught this before attempting a 5-minute build.

**What the current service checks (`docker_compose.py`):**
- `compose_file_exists()` — is there a `docker-compose.yml`?
- `is_compose_running()` — are any services already up?
- `get_service_health()` — per-service `state`/`health` from `docker compose ps`
- `test_endpoints()` — HTTP probe of `:5001/health`, `:5002/health`, `:8080/`

**What is not checked at all before launching a build:**

### Proposed: `validate_compose_config()` — run before any `up --build`

#### 1. Config parse check
Run `docker compose config` and capture exit code + stderr before touching images or network. Surfaces YAML errors and missing env vars immediately.

```python
result = subprocess.run(['docker', 'compose', '-f', compose_file, 'config', '--quiet'],
                        capture_output=True, text=True)
# Non-zero = malformed compose file or unresolved variable
```

#### 2. Build context existence check
Parse the resolved config JSON (`docker compose config --format json`) and for every service with a `build.context`, verify the path exists on disk:

```python
config = json.loads(subprocess.check_output(['docker', 'compose', 'config', '--format', 'json']))
for name, svc in config.get('services', {}).items():
    ctx = (svc.get('build') or {}).get('context')
    if ctx and not os.path.isdir(ctx):
        issues.append(f"  ✗ {name}: build context not found: {ctx}")
```

#### 3. Dockerfile existence check
For each service with a build block, check that `<context>/<dockerfile>` resolves to a real file:

```python
dockerfile = (svc.get('build') or {}).get('dockerfile', 'Dockerfile')
full_path = os.path.join(ctx, dockerfile)
if not os.path.isfile(full_path):
    issues.append(f"  ✗ {name}: Dockerfile not found: {full_path}")
```

#### 4. Port conflict check
Before `up`, scan each service's published ports against `lsof -i :<port>` or `socket.create_connection`. If a port is already bound by a non-compose process (e.g. `dotnet run` holding `:5001`), warn before Docker errors out mid-launch:

```
[Port conflicts]
  ✗ :5001 already in use (PID 12345 — dotnet)  → kill $(lsof -ti :5001)
  ✓ :5002 free
  ✓ :5433 free
```

#### 5. Required image availability check
For services using `image:` (not `build:`), verify the image exists in Docker or is pullable before starting:

```python
for name, svc in services.items():
    if 'image' in svc and 'build' not in svc:
        result = subprocess.run(['docker', 'image', 'inspect', svc['image']], capture_output=True)
        if result.returncode != 0:
            issues.append(f"  ✗ {name}: image {svc['image']!r} not present locally — will pull on start")
```

#### 6. Environment variable check
Collect all `${VAR:-default}` references from the compose config. Flag any that have no default and are not set in the environment or a `.env` file:

```
[Env vars]
  ✓ SIMULATION_API_KEY   (default: dev-key-change-in-production)
  ✓ ASPNETCORE_ENVIRONMENT   (default: Development)
  ✗ JWT_SECRET_OVERRIDE  → no default, not in .env  (service: backend1)
```

### Where to wire this in

| Trigger | Action |
|---|---|
| CLI: Docker Compose menu → "Build and Start" | Run all 6 checks, show table, prompt to continue or abort |
| CLI: `smartprobe setup` (item 8 below) | Run checks 1–3 only (build context + dockerfile) — fast, no port scan needed |
| Drift detection (`run_drift_checks`) | Add check 4 (port conflicts) as a new finding that surfaces before the user tries to start compose |

### Output format

```
[Docker Compose Pre-launch]
  ✓ docker-compose.yml parsed OK
  ✓ backend1  build context: ../asset-manager-1           exists
  ✓ backend2  build context: ../asset-manager-1           exists
  ✓ frontend  build context: ../asset-manager-1/frontend  exists
  ✓ simulation  Dockerfile: Dockerfile.simulation         exists
  ✗ :5001  already bound (dotnet PID 9821)  → kill $(lsof -ti :5001)
  ✓ :5002  free
  ✓ :5433  free
  ✓ postgres:16  image present locally

  1 issue found.  Fix before launching? [Y/n]
```

---

## 9. Feature Request: `diagnose image` CLI Command

**Summary:** A dedicated interactive CLI command that walks through the full image lifecycle for a given image name — from local Docker build cache through to cluster-node resolution — and tells the developer exactly where the chain is broken and how to fix it.

**Entry point:** New welcome-menu option **"Diagnose Image"** (or surfaced automatically when `ErrImagePull` / `ErrImageNeverPull` is detected on any pod).

---

### Problem This Solves

Image-related pod failures have multiple distinct root causes, each requiring a completely different fix, but all surfacing as the same pod status. Without this command a developer must run 5–6 manual commands across Docker, the local registry, and kubectl to locate the break:

| Symptom | Actual cause |
|---|---|
| `ErrImageNeverPull` | `pullPolicy: Never` but image not in node's containerd |
| `ErrImagePull` (registry-mirror 500) | Node's containerd doesn't trust the HTTP registry |
| `ErrImagePull` (manifest unknown) | Image was never pushed to the registry |
| `ErrImagePull` (connection refused) | Local registry container isn't running |
| Pod works on one node, fails another | Image loaded on some nodes but not others |

---

### Command Behaviour

```
? What would you like to do? Diagnose Image

? Image to diagnose: (playwright-agent:latest)

[1/7] Docker daemon reachable...          ✓
[2/7] Image in local Docker cache...      ✓  sha256:42154f5b  (156 MB, built 3h ago)
[3/7] Local registry reachable (:5050)... ✓  catalog: ['playwright-agent']
[4/7] Image pushed to registry...         ✓  host.docker.internal:5050/playwright-agent:latest
[5/7] Cluster context...                  ✓  docker-desktop (10 nodes, containerd 2.2.0)
[6/7] Containerd registry trust...        ✗  7/10 nodes patched — 3 nodes missing config
         desktop-worker   → not patched
         desktop-worker2  → not patched
         desktop-worker9  → not patched
[7/7] Pull test from cluster...           ✗  SKIP — trust not established on all nodes

Diagnosis: nodes not fully patched.

? What would you like to do?
  » Patch missing nodes (deploy registry-config-patch DaemonSet)
    Re-run diagnosis
    Show raw containerd config for a node
    Back
```

---

### Stage Definitions

| Stage | What it checks | Method |
|---|---|---|
| 1. Docker daemon | Reachable via Unix socket | `socket.connect('/var/run/docker.sock')` |
| 2. Local cache | Image exists in Docker with expected tag | `docker image inspect <image>` → parse `Id`, `Size`, `Created` |
| 3. Registry reachable | HTTP GET `http://localhost:5050/v2/_catalog` | `urllib.request.urlopen` timeout=3 |
| 4. Image pushed | Repository name present in catalog + HEAD `/v2/<name>/manifests/<tag>` returns 200 | HTTP HEAD request |
| 5. Cluster context | `kubectl config current-context` + node count + containerd version | `kubectl get nodes -o json` |
| 6. Registry trust | Per-node: check if `host.docker.internal:5050` appears in `/etc/containerd/config.toml` via DaemonSet init-container log | `kubectl logs -n kube-system <patch-pod> -c patch-containerd` — look for "Done" / "Already patched" |
| 7. Pull test | Schedule a one-shot pod using the image on a random worker node, wait 10 s for status | `kubectl run diagnose-pull-<uuid> --image=<registry-image> --restart=Never --image-pull-policy=IfNotPresent` → check pod phase |

Short-circuit rules:
- Stage 1 fails → skip everything, print Docker fix
- Stage 2 fails → skip 4–7 (nothing to push or pull)
- Stage 3 fails → skip 4, 7 (can't test pull)
- Stage 6 partial → run stage 7 only on a known-patched node

---

### Fix Actions (One-Key Remediation)

| Problem detected | Offered fix |
|---|---|
| Docker daemon down | Print `open -a Docker` (macOS) / `systemctl start docker` |
| Image not in local cache | `docker build -t <image> .` — prompt for Dockerfile path |
| Image not in registry | `docker tag <image> localhost:5050/<image> && docker push localhost:5050/<image>` |
| Registry not running | `docker run -d -p 5050:5000 --name local-registry registry:2` |
| Nodes not trusted | Deploy `registry-config-patch` DaemonSet, poll until all nodes complete |
| Pull test fails | Show raw pod events (`kubectl describe pod diagnose-pull-<uuid>`) |

---

### Integration Points

- **Welcome menu drift check**: if any pods have `ErrImagePull` or `ErrImageNeverPull`, surface a `⚠ Image pull errors detected` finding with `→ Run "Diagnose Image" to locate the break`
- **Start Test flow**: run stages 1–5 silently before helm install; if trust check shows < 100% patched nodes, warn and offer to fix before deploying
- **Agent Logs menu**: when a selected pod is in `ErrImagePull`/`ErrImageNeverPull`, prepend a banner with a direct link to this command
- **`smartprobe setup`**: runs stages 1–6 as step [2/6] and [3/6] (already planned in item 8)

---

### Implementation Notes

- Stage 6 (trust check) relies on the `registry-config-patch` DaemonSet being present. If it isn't, fall back to checking containerd config via a privileged `kubectl debug node` pod — but this is slow (30 s per node). Only do this when the DaemonSet is absent.
- Stage 7 (pull test) creates a real pod — clean it up unconditionally in a `finally` block: `kubectl delete pod diagnose-pull-<uuid> --ignore-not-found`.
- The command should accept an optional `--image` argument so it can be called non-interactively from CI or the `smartprobe setup` flow.
- Cache stage 2–4 results in session memory so repeated runs within the same CLI session don't re-probe Docker/registry on every invocation.

---

## 8. Quick-action Order (Suggested CLI "Setup" Flow)

```
smartprobe setup

  [1/6] Checking tooling (kubectl, helm, docker, kind)...     ✓
  [2/6] Checking Docker images...                             ✓ playwright-agent:latest present
  [3/6] Checking local registry (:5050)...                    ✓ running, image pushed
  [4/6] Patching cluster nodes (insecure registry trust)...   ✓ 10/10 nodes patched
  [5/6] Validating docker-compose.yml...                      ✗ :5001 already bound (dotnet)
  [6/6] Checking service health (5001, 5002, 5003)...         ✗ backend not reachable

  Setup complete with 2 warnings.
  → Kill port: kill $(lsof -ti :5001)
  → Start backend: cd asset-manager-1/backend && dotnet run
```

All steps idempotent — safe to re-run any time something looks broken.
