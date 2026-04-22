"""
Playwright Agent — supports two modes:

  basic        — simple HTTP probe that verifies the website loads (HTTP 200,
                 page contains expected content).  No auth, no coordinator.

  transactional — coordinated end-to-end simulation: agents register, get
                  assigned roles (seller/buyer/observer), and interact with each
                  other through the asset management platform:
                    - Sellers:   create and list assets in the shared pool
                    - Buyers:    claim & transfer assets (exercises race conditions)
                    - Observers: verify data consistency across the system

Each agent reports real-time actions to the simulation activity feed.
"""

import os
import time
import random
import json
import socket
import requests

# ─── Configuration ───────────────────────────────────────────────────────────
TARGET_URL = os.getenv("TARGET_URL", "http://frontend-service")
BACKEND_API = os.getenv("BACKEND_API", "http://host.docker.internal:5001/api/simulation/results")
SIM_API = os.getenv("SIM_API", "http://host.docker.internal:5002/api/simulation")
COORD_API = os.getenv("COORD_API", "http://host.docker.internal:5003/api/coordinator")
PERSONA = os.getenv("AGENT_PERSONA", "browser")
THINK_TIME = float(os.getenv("THINK_TIME", "2"))
POD_NAME = os.getenv("HOSTNAME", socket.gethostname())
PROBE_MODE = os.getenv("PROBE_MODE", "transactional")
AGENT_ROUNDS = int(os.getenv("AGENT_ROUNDS", "3"))
ROUND_SLEEP = float(os.getenv("ROUND_SLEEP", "30"))

# Backend API base (same host as TARGET_URL for the C# backend)
API_BASE = TARGET_URL.rstrip("/")

# Transfer Stacker item catalog
TS_ITEMS = ["Widget A", "Widget B", "Gasket", "Steel Rod", "Relay", "Piston", "Valve"]

print(f"[agent] starting: {PERSONA} ({POD_NAME})")
print(f"[agent] mode: {PROBE_MODE}")
print(f"[agent] target: {TARGET_URL}, sim: {SIM_API}")
if PROBE_MODE != "basic":
    print(f"[agent] coordinator: {COORD_API}")


# ─── Helpers ─────────────────────────────────────────────────────────────────
def report_action(action: str, details: str = None):
    """Report a real-time action to the simulation activity feed."""
    try:
        requests.post(
            f"{SIM_API}/agent-action",
            json={"pod": POD_NAME, "action": action, "details": details},
            timeout=3,
        )
    except Exception:
        pass


def send_result(status, actions, duration_ms=0, error=None):
    result = {
        "pod": POD_NAME,
        "persona": PERSONA,
        "status": status,
        "actions": actions,
        "durationMs": duration_ms,
    }
    if error:
        result["error"] = str(error)
    try:
        requests.post(BACKEND_API, json=result, timeout=5)
    except Exception as e:
        print(f"[agent] result send failed: {e}")
    try:
        requests.post(f"{SIM_API}/agent-result", json=result, timeout=3)
    except Exception:
        pass


def think(lo=None, hi=None):
    """Simulate human think time."""
    lo = lo if lo is not None else THINK_TIME * 0.5
    hi = hi if hi is not None else THINK_TIME * 2
    time.sleep(random.uniform(lo, hi))


def api_get(path, token=None, timeout=10):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.get(f"{API_BASE}{path}", headers=headers, timeout=timeout)


def api_post(path, body, token=None, timeout=10):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.post(f"{API_BASE}{path}", json=body, headers=headers, timeout=timeout)


def coord_get(path, params=None):
    r = requests.get(f"{COORD_API}{path}", params=params, timeout=5)
    if not r.content:
        raise ValueError(f"empty response (HTTP {r.status_code}) from {COORD_API}{path}")
    return r


def coord_post(path, body):
    r = requests.post(f"{COORD_API}{path}", json=body, timeout=5)
    if not r.content:
        raise ValueError(f"empty response (HTTP {r.status_code}) from {COORD_API}{path}")
    return r


# ─── Auth ────────────────────────────────────────────────────────────────────
def register_and_login():
    """Create a unique agent account and login to get JWT + user_id + password."""
    agent_id = f"agent-{POD_NAME[-8:]}-{random.randint(1000, 9999)}"
    username = agent_id
    email = f"{agent_id}@sim.local"
    password = "SimPass123!"

    # Register
    try:
        r = api_post("/api/auth/register", {
            "username": username, "email": email, "password": password
        })
        if r.status_code in (200, 201):
            print(f"[agent] registered: {username}")
            report_action("registered", username)
        else:
            print(f"[agent] register {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[agent] register failed: {e}")

    think(0.3, 0.8)

    # Login
    try:
        r = api_post("/api/auth/login", {"username": username, "password": password})
        if r.status_code == 200:
            data = r.json()
            token = data.get("token")
            user_id = str(data.get("userId"))
            print(f"[agent] logged in: {username} (uid={user_id})")
            report_action("logged_in", username)
            return token, user_id, username, password
        else:
            print(f"[agent] login {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[agent] login failed: {e}")

    return None, None, username, password


def relogin(username, password):
    """Get a fresh token using existing credentials."""
    try:
        r = api_post("/api/auth/login", {"username": username, "password": password})
        if r.status_code == 200:
            data = r.json()
            return data.get("token"), str(data.get("userId"))
    except Exception as e:
        print(f"[agent] re-login failed: {e}")
    return None, None


# ─── Coordinator Registration ────────────────────────────────────────────────
def register_with_coordinator(user_id, username, token):
    """Register with the coordinator and get role assignment."""
    try:
        r = coord_post("/register", {
            "pod": POD_NAME,
            "user_id": user_id,
            "username": username,
            "token": token,
        })
        data = r.json()
        role = data.get("role", "buyer")
        print(f"[agent] coordinator role: {role} (agents: {data.get('agent_count')})")
        return role
    except Exception as e:
        print(f"[agent] coordinator register failed: {e}")
        return "buyer"


# ─── Stack Generator ─────────────────────────────────────────────────────────
def generate_stack(username):
    """Generate a random stack payload for Transfer Stacker."""
    items = [
        {"name": random.choice(TS_ITEMS), "qty": random.randint(1, 5)}
        for _ in range(random.randint(1, 3))
    ]
    return {
        "name": f"Stack from {username}",
        "items": items,
        "createdBy": username,
    }


# ─── Role Scenarios ──────────────────────────────────────────────────────────
def run_seller(token, user_id, username):
    """Seller: create 2-4 stacks and register them in the shared pool."""
    actions = []
    num_stacks = random.randint(2, 4)

    for i in range(num_stacks):
        stack_data = generate_stack(username)
        item_summary = ", ".join(f"{it['name']} x{it['qty']}" for it in stack_data["items"])
        try:
            r = api_post("/stacks", stack_data, token=token)
            if r.status_code in (200, 201):
                created = r.json()
                stack_id = created.get("id")
                actions.append(f"created:{stack_id}")
                report_action("asset_created", f"{stack_data['name']} — {username} — {item_summary}")

                # Register in coordinator pool so buyers can find it
                coord_post("/assets", {
                    "asset_id": stack_id,
                    "owner_id": user_id,
                    "name": stack_data["name"],
                    "price": 0,
                })
                print(f"[seller] created stack #{stack_id}: {item_summary}")
            else:
                print(f"[seller] create failed {r.status_code}: {r.text[:120]}")
                actions.append("create_failed")
        except Exception as e:
            print(f"[seller] error: {e}")
            actions.append("create_error")

        think(0.5, 1.5)

    return actions


def run_buyer(token, user_id, username):
    """Buyer: find open stacks, claim one, execute transfer."""
    actions = []
    max_attempts = 3

    for attempt in range(max_attempts):
        think(1, 3)

        # Check coordinator pool first (only contains stacks created by seller agents)
        pool = []
        try:
            r = coord_get("/assets", params={"exclude_owner": user_id})
            pool = r.json().get("assets", [])
        except Exception as e:
            print(f"[buyer] coord pool fetch failed: {e}")

        if not pool:
            # Fall back: scan /stacks directly for any open stack we didn't create
            try:
                r = api_get("/stacks", token=token)
                if r.status_code == 200:
                    all_stacks = r.json()
                    open_stacks = [
                        s for s in all_stacks
                        if s.get("status") == "open"
                        and s.get("createdByUserId") != user_id
                    ]
                    if open_stacks:
                        target_stack = random.choice(open_stacks)
                        stack_id = target_stack["id"]
                        item_summary = ", ".join(
                            f"{i['name']} x{i['qty']}" for i in target_stack.get("items", [])
                        )
                        report_action("transfer_started",
                                      f"{target_stack['name']} — {target_stack.get('createdByUsername', '?')} → {username}")
                        r2 = api_post(f"/stacks/{stack_id}/transfer",
                                      {"recipient": username}, token=token)
                        if r2.status_code == 200:
                            report_action("transfer_completed",
                                          f"{target_stack['name']} → {username} — {item_summary}")
                            actions.append(f"transferred:{stack_id}")
                            print(f"[buyer] direct transfer OK: stack #{stack_id}")
                        else:
                            actions.append(f"transfer_failed:{stack_id}")
                        continue
            except Exception as e:
                print(f"[buyer] direct scan error: {e}")

            report_action("asset_listed", f"pool empty (attempt {attempt + 1})")
            actions.append("pool_empty")
            think(2, 4)
            continue

        # Claim from coordinator pool (race-condition safe)
        target = random.choice(pool)
        asset_id = target["asset_id"]
        seller_id = target["owner_id"]
        report_action("transfer_started", f"claiming {target['name']}")

        try:
            r = coord_post("/claim", {
                "asset_id": asset_id,
                "pod": POD_NAME,
                "buyer_id": user_id,
            })
            result = r.json()

            if result.get("status") == "conflict":
                print(f"[buyer] CONFLICT: {target['name']} already claimed")
                report_action("conflict_detected", f"{target['name']} — claimed by another agent")
                actions.append(f"conflict:{asset_id}")
                continue

            # Execute the actual transfer
            print(f"[buyer] claimed: {target['name']} — executing transfer")
            r = api_post(f"/stacks/{asset_id}/transfer",
                         {"recipient": username}, token=token)
            if r.status_code == 200:
                report_action("transfer_completed", f"{target['name']} → {username}")
                coord_post("/transaction", {
                    "asset_id": asset_id, "from": seller_id,
                    "to": user_id, "status": "completed",
                })
                actions.append(f"transferred:{asset_id}")
                print(f"[buyer] transfer SUCCESS: {target['name']}")
            else:
                report_action("transfer_failed", f"{target['name']} — {r.status_code}")
                coord_post("/transaction", {
                    "asset_id": asset_id, "from": seller_id,
                    "to": user_id, "status": "failed",
                })
                actions.append(f"transfer_failed:{asset_id}")
                print(f"[buyer] transfer FAILED: {r.status_code}")
        except Exception as e:
            print(f"[buyer] claim error: {e}")
            actions.append("claim_error")

    return actions


def run_observer(token, user_id, username):
    """Observer: verify stack counts and transfer consistency."""
    actions = []

    think(2, 5)  # let sellers/buyers get ahead

    try:
        r = api_get("/stacks", token=token)
        if r.status_code == 200:
            all_stacks = r.json()
            open_count = sum(1 for s in all_stacks if s.get("status") == "open")
            transferred_count = sum(1 for s in all_stacks if s.get("status") == "transferred")
            owners = set(s.get("createdByUserId") for s in all_stacks if s.get("createdByUserId"))
            report_action("consistency_check",
                          f"{len(all_stacks)} stacks: {open_count} open, {transferred_count} transferred, {len(owners)} creators")
            actions.append(f"observed:{len(all_stacks)}_stacks_{open_count}_open")
            print(f"[observer] {len(all_stacks)} stacks: {open_count} open, {transferred_count} transferred")

            # Cross-check with coordinator
            try:
                r2 = coord_get("/stats")
                stats = r2.json()
                coord_total = stats.get("transactions", {}).get("total", 0)
                conflicts = stats.get("transactions", {}).get("conflicts", 0)
                report_action("consistency_check",
                              f"coord txns:{coord_total} conflicts:{conflicts} pool:{stats.get('pool_size', 0)}")
                actions.append(f"verified:txns={coord_total},conflicts={conflicts}")
            except Exception:
                actions.append("coord_stats_error")
        else:
            actions.append(f"observe_failed:{r.status_code}")
    except Exception as e:
        print(f"[observer] error: {e}")
        actions.append("observe_error")

    return actions


# ─── Basic Probe ─────────────────────────────────────────────────────────────
# Verifies the website loads (HTTP 200) without auth or coordinator interaction.
# Used when PROBE_MODE=basic.

def run_basic_probe():
    """Load the target URL in a real browser and verify the login page loads (non-404)."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    start = time.time()
    # PROBE_URL is set by the Helm chart (probeUrl value); fall back to TARGET_URL
    url = os.getenv("PROBE_URL", os.getenv("TARGET_URL", "http://localhost:5174")).rstrip("/")

    print(f"[probe] browser load → {url}")
    report_action("probe_start", f"target={url}")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
            try:
                response = page.goto(url, timeout=15000, wait_until="domcontentloaded")
                status = response.status if response else 0
                ok = status != 0 and status != 404
                label = "ok" if ok else f"http_{status}"
                print(f"[probe] {status} → {label}")
                report_action("probe_get", f"{url} → {status}")
                actions = [f"load:{label}"]
            except PWTimeout:
                print(f"[probe] TIMEOUT → {url}")
                report_action("probe_error", f"{url} → timeout")
                actions = ["load:timeout"]
            finally:
                browser.close()
    except Exception as exc:
        print(f"[probe] FAIL → {exc}")
        report_action("probe_error", f"{url} → {exc}")
        actions = ["load:error"]

    duration_ms = int((time.time() - start) * 1000)
    passed = actions and "ok" in actions[0]
    report_action("probe_done", f"{'ok' if passed else 'failed'} in {duration_ms}ms")
    print(f"[probe] done in {duration_ms}ms: {actions}")
    return actions, duration_ms


# ─── Main Agent Loop ─────────────────────────────────────────────────────────
def run_agent():
    start = time.time()
    all_actions = []

    # 1. Register once and get a persistent identity
    token, user_id, username, password = register_and_login()
    if not token:
        return ["auth_failed"], int((time.time() - start) * 1000)
    all_actions.append("authenticated")

    think(0.5, 1)

    # 2. Register with coordinator and get role (stays the same across rounds)
    role = register_with_coordinator(user_id, username, token)
    all_actions.append(f"role:{role}")

    # 3. Run for AGENT_ROUNDS rounds, sleeping and re-logging between each
    for round_num in range(AGENT_ROUNDS):
        if round_num > 0:
            print(f"[agent] sleeping {ROUND_SLEEP}s before round {round_num + 1}/{AGENT_ROUNDS}")
            report_action("agent_sleep",
                          f"{username} sleeping {ROUND_SLEEP}s (round {round_num + 1}/{AGENT_ROUNDS})")
            time.sleep(ROUND_SLEEP)

            # Re-login for a fresh token
            new_token, new_uid = relogin(username, password)
            if new_token:
                token, user_id = new_token, new_uid
                report_action("agent_relogin", f"{username} round {round_num + 1}")

        print(f"[agent] round {round_num + 1}/{AGENT_ROUNDS} — role:{role}")
        report_action("round_start", f"round {round_num + 1}/{AGENT_ROUNDS} role:{role}")

        if role == "seller":
            actions = run_seller(token, user_id, username)
        elif role == "buyer":
            actions = run_buyer(token, user_id, username)
        elif role == "observer":
            actions = run_observer(token, user_id, username)
        else:
            actions = ["unknown_role"]

        all_actions.extend(actions)
        think(1, 2)

    duration_ms = int((time.time() - start) * 1000)

    # Summary report
    tx_counts = {k: sum(1 for a in all_actions if a.startswith(k))
                 for k in ('transferred:', 'conflict:', 'created:')}
    summary_parts = [f"role:{role}", f"{AGENT_ROUNDS} rounds"]
    if tx_counts['created:']:
        summary_parts.append(f"created {tx_counts['created:']} stacks")
    if tx_counts['transferred:']:
        summary_parts.append(f"transferred {tx_counts['transferred:']} stacks")
    if tx_counts['conflict:']:
        summary_parts.append(f"{tx_counts['conflict:']} conflicts")
    summary_parts.append(f"{duration_ms // 1000}s")
    report_action("agent_done", " · ".join(summary_parts))

    return all_actions, duration_ms


if __name__ == "__main__":
    try:
        if PROBE_MODE == "basic":
            actions, duration_ms = run_basic_probe()
            status = "completed" if actions and "ok" in actions[0] else "partial"
        else:
            actions, duration_ms = run_agent()
            status = "completed" if not any("error" in a or "failed" in a for a in actions) else "partial"
        send_result(status, actions, duration_ms)
        print(f"[agent] done in {duration_ms}ms: {actions}")
    except Exception as e:
        print(f"[agent] FATAL: {e}")
        send_result("error", [], 0, str(e))
