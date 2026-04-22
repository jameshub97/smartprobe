# Flask Architecture: Blueprint Migration

This document describes the blueprint structure for `simulation_service.py` — the target architecture, the known issues in the current monolith, and the step-by-step migration plan.

---

## Current State: The Monolith

`simulation_service.py` started at 2636 lines. After addressing the technical debt below it is 2537 lines, with all 34 routes live on a single Flask app instance.

### ~~Critical Bug: Two Flask App Instances~~ — FIXED

The file previously created `app = Flask(__name__)` **three times** — at line ~70, ~710, and ~1192. Any route registered before the final re-assignment (including all `/api/cleanup/*` endpoints and `/metrics`) was registered on a dead app object that was immediately overwritten. Those routes were never served.

**What was removed:**
- Duplicate imports + second `app = Flask(...)` at line ~710 (originally from a code-paste)
- Duplicate shebang + imports + third `app = Flask(...)` at line ~1192 (another embedded copy)
- Duplicate `@app.route('/metrics')` / `prometheus_metrics()` function (second copy)
- Duplicate `require_api_key` decorator and `SIMULATION_API_KEY` constant (second copy)
- Duplicate `is_valid_release_name`, `is_valid_persona`, `SIMULATION_MODES`, `PRESETS` (second copy)
- Unused `import shlex` and scattered `_re` alias (consolidated to top-level `import re`)

**Routes now live (were dead before):**

| Route | Method |
|---|---|
| `GET /metrics` | Prometheus scrape endpoint |
| `POST /api/cleanup/all` | Full cluster cleanup |
| `DELETE /api/cleanup/release/<name>` | Release-specific cleanup |
| `POST /api/cleanup/stuck` | PVC/PDB cleanup |
| `POST /api/cleanup/reset` | Full cluster reset |
| `GET /api/cleanup/verify` | Clean state check |
| `GET /api/cleanup/preflight` | Preflight via cleanup handler |
| `GET /api/diagnostics/*` | All six diagnostics endpoints |

This also explains why the CLI always fell back to direct mode for cleanup operations — the Flask endpoints they targeted had never been registered on the running app.

---

## Target Architecture: Blueprints

Each logical group of routes becomes a standalone Python module (a Flask Blueprint). The main file shrinks to app creation + middleware setup + blueprint registration.

```
simulation_service.py          ← app factory, middleware, startup only
routes/
    __init__.py                ← register_blueprints(app) helper
    dashboard.py               ← GET /  GET /dashboard  /favicon.*
    preflight.py               ← GET /api/preflight
    cleanup.py                 ← POST|DELETE /api/cleanup/*
    simulation.py              ← /health  /api/simulation/*  /api/simulation/start|stop
    diagnostics.py             ← /api/diagnostics/*
```

### Blueprint files (created)

All five blueprint files exist at `routes/` and contain the extracted route handlers. They currently import shared state directly from `simulation_service` — see the [Shared State](#shared-state) section for the next migration step.

---

## How to Register Blueprints

Once `simulation_service.py` is refactored to a single clean app instance, registration is one function call:

```python
# simulation_service.py (after migration)
from flask import Flask
from flask_cors import CORS
from routes import register_blueprints

app = Flask(__name__)
CORS(app, origins=[...])

register_blueprints(app)
```

Or register individually to control order:

```python
from routes.dashboard import dashboard_bp
from routes.preflight import preflight_bp
from routes.cleanup import cleanup_bp
from routes.simulation import simulation_bp
from routes.diagnostics import diagnostics_bp

for bp in (dashboard_bp, preflight_bp, cleanup_bp, simulation_bp, diagnostics_bp):
    app.register_blueprint(bp)
```

---

## Shared State

The biggest challenge in the migration is shared mutable state: `_activity_log`, `_agent_states`, `_agent_results`, `_cache`, `k8s_monitor`, `cleanup_handler`, Prometheus counters/gauges, etc. These currently live as module-level globals in `simulation_service.py`.

The clean solution is a `routes/state.py` module that owns all shared state:

```python
# routes/state.py
from collections import deque, defaultdict

_activity_log = deque(maxlen=500)
_agent_states = {}
_agent_results = []
_event_totals = defaultdict(int)
_cache = {'data': None, 'timestamp': None, 'ttl': 2.0}

k8s_monitor = None       # set by create_app()
cleanup_handler = None   # set by create_app()
```

Each blueprint then imports from `routes.state` rather than from `simulation_service`:

```python
# routes/simulation.py
from routes.state import _activity_log, _agent_states, k8s_monitor
```

---

## Migration Steps

### Step 1 — Write API route tests (before touching anything)

Create `tests/test_api_routes.py` using Flask's test client. Mock `run_cli_command` and `K8sSimulationMonitor` so tests run without a real cluster:

```python
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr('simulation_service.run_cli_command', mock_run)
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c

def test_preflight_ok(client):
    r = client.get('/api/preflight')
    assert r.status_code == 200
    assert 'has_conflicts' in r.json
```

Coverage needed before any refactoring:
- `GET /health`
- `GET /api/preflight`
- `POST /api/simulation/start` (valid + invalid inputs)
- `POST /api/cleanup/stuck`
- `GET /api/simulation/summary`

### Step 2 — Fix the dual-app bug

Remove the second `app = Flask(__name__)` at line ~710. All cleanup routes (currently dead) become live because they're now on the single app instance.

This is a one-line deletion but requires the Step 1 tests to confirm nothing breaks.

### Step 3 — Extract `K8sSimulationMonitor` and `ClusterCleanup`

Move these two classes to `simulation_service/monitor.py` and `simulation_service/cleanup.py`. Import them back into `simulation_service.py` to keep the existing surface unchanged during this step.

### Step 4 — Extract shared state to `routes/state.py`

Create the state module. Update blueprint files to import from it instead of from `simulation_service`. Run tests.

### Step 5 — Register blueprints

Replace the inline route handlers in `simulation_service.py` with `register_blueprints(app)`. Delete the now-duplicate handler functions from the monolith.

### Step 6 — Shrink simulation_service.py

What remains in `simulation_service.py` after full migration:
- App factory
- CORS config
- Prometheus metrics setup
- `@before_request` / `@after_request` middleware
- Blueprint registration
- `main()` CLI entry point
- Background threads (`_background_updater`)

Target size: ~200 lines.

---

## Adding New Endpoints (Post-Migration)

Once blueprints are registered, adding a new feature (e.g. `GET /api/diagnose/image` from DEV_EX_DIAGNOSTICS Section 9) is:

1. Add the handler to `routes/diagnostics.py`
2. No changes to `simulation_service.py`
3. No risk to existing routes

```python
@diagnostics_bp.route('/api/diagnostics/image', methods=['GET'])
def diagnose_image():
    # ... implementation
    return jsonify(result)
```

---

## Route Map (Full)

| Blueprint | Method | Path | Auth |
|---|---|---|---|
| `dashboard` | GET | `/` | — |
| `dashboard` | GET | `/dashboard` | — |
| `dashboard` | GET | `/favicon.svg` | — |
| `dashboard` | GET | `/favicon.ico` | — |
| `preflight` | GET | `/api/preflight` | — |
| `cleanup` | POST | `/api/cleanup/all` | Bearer |
| `cleanup` | DELETE | `/api/cleanup/release/<name>` | Bearer |
| `cleanup` | POST | `/api/cleanup/stuck` | Bearer |
| `cleanup` | POST | `/api/cleanup/reset` | Bearer |
| `cleanup` | GET | `/api/cleanup/verify` | — |
| `cleanup` | GET | `/api/cleanup/preflight` | — |
| `simulation` | GET | `/health` | — |
| `simulation` | GET | `/api/simulation/summary` | — |
| `simulation` | GET | `/api/simulation/activity` | — |
| `simulation` | POST | `/api/simulation/agent-action` | — |
| `simulation` | GET | `/api/simulation/agent-states` | — |
| `simulation` | GET | `/api/simulation/live-logs` | — |
| `simulation` | POST | `/api/simulation/agent-result` | — |
| `simulation` | GET | `/api/simulation/agent-results` | — |
| `simulation` | GET | `/api/simulation/agent-detail/<pod>` | — |
| `simulation` | GET | `/api/simulation/pod-logs/<pod>` | — |
| `simulation` | POST | `/api/simulation/start` | Bearer |
| `simulation` | POST | `/api/simulation/stop` | Bearer |
| `simulation` | GET | `/api/simulation/tests` | — |
| `simulation` | GET | `/api/simulation/presets` | — |
| `simulation` | GET | `/api/simulation/status` | — |
| `diagnostics` | GET | `/api/diagnostics/deployment/<release>` | — |
| `diagnostics` | GET | `/api/diagnostics/performance/<release>` | — |
| `diagnostics` | GET | `/api/diagnostics/network/<release>` | — |
| `diagnostics` | GET | `/api/diagnostics/progress/<release>` | — |
| `diagnostics` | GET | `/api/diagnostics/cluster` | — |
| `diagnostics` | GET | `/api/diagnostics/cost/<release>` | — |
| *(app-level)* | GET | `/metrics` | — |
