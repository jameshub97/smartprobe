"""
Coordinated Playwright Agent — End-to-End Distributed Interaction

Agents register with a coordinator, get assigned roles (seller/buyer/observer),
and interact with each other through the asset management platform:
  - Sellers: register, login, create assets, list them in the shared pool
  - Buyers:  register, login, browse pool, claim & transfer assets (race conditions)
  - Observers: register, login, verify data consistency across the system

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

# Backend API base (same host as TARGET_URL for the C# backend)
API_BASE = TARGET_URL.rstrip("/")

print(f"[agent] starting: {PERSONA} ({POD_NAME})")
print(f"[agent] target: {TARGET_URL}, sim: {SIM_API}")
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
    """Create a unique agent account and login to get JWT + user_id."""
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
            return token, user_id, username
        else:
            print(f"[agent] login {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[agent] login failed: {e}")

    return None, None, username


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


# ─── Asset Name Generator ───────────────────────────────────────────────────
ASSET_NAMES = [
    "Solar Panel Array", "Wind Turbine Unit", "Battery Storage Module",
    "Server Rack Unit", "Network Switch", "Load Balancer Appliance",
    "GPU Compute Node", "Storage NAS Cluster", "Fiber Optic Cable Set",
    "UPS Backup System", "Cooling Unit", "Power Distribution Unit",
    "Security Camera Kit", "Access Control Panel", "Fire Suppression Unit",
    "Diesel Generator", "Transformer Station", "Smart Meter Array",
    "Edge Computing Node", "IoT Gateway Device",
]


def generate_asset():
    """Generate a random asset."""
    name = random.choice(ASSET_NAMES)
    suffix = random.randint(100, 999)
    return {
        "name": f"{name} #{suffix}",
        "description": f"Auto-generated asset by {POD_NAME}",
        "price": round(random.uniform(50, 5000), 2),
    }


# ─── Role Scenarios ──────────────────────────────────────────────────────────
def run_seller(token, user_id):
    """Seller: create 2-4 assets and register them in the shared pool."""
    actions = []
    num_assets = random.randint(2, 4)

    for i in range(num_assets):
        asset_data = generate_asset()
        try:
            r = api_post("/api/assets", asset_data, token=token)
            if r.status_code in (200, 201):
                created = r.json()
                asset_id = created.get("id")
                actions.append(f"created:{asset_id}")
                report_action("asset_created", f"{asset_data['name']} (${asset_data['price']:.0f})")

                # Register in coordinator pool
                coord_post("/assets", {
                    "asset_id": asset_id,
                    "owner_id": user_id,
                    "name": asset_data["name"],
                    "price": asset_data["price"],
                })
                print(f"[seller] created asset: {asset_data['name']} -> {asset_id}")
            else:
                print(f"[seller] create failed {r.status_code}: {r.text[:120]}")
                actions.append("create_failed")
        except Exception as e:
            print(f"[seller] error: {e}")
            actions.append("create_error")

        think(0.5, 1.5)

    # Also browse the asset list to simulate mixed usage
    try:
        r = api_get("/api/assets?page=1&pageSize=20", token=token)
        if r.status_code == 200:
            data = r.json()
            total = data.get("totalCount", 0)
            report_action("asset_listed", f"browsed {total} assets")
            actions.append(f"browsed:{total}")
    except Exception:
        pass

    return actions


def run_buyer(token, user_id):
    """Buyer: browse shared pool, claim assets, execute transfers."""
    actions = []
    max_attempts = 3

    for attempt in range(max_attempts):
        think(1, 3)

        # Check coordinator pool for available assets
        try:
            r = coord_get("/assets", params={"exclude_owner": user_id})
            pool = r.json().get("assets", [])
        except Exception as e:
            print(f"[buyer] pool fetch failed: {e}")
            actions.append("pool_error")
            continue

        if not pool:
            report_action("asset_listed", f"pool empty (attempt {attempt + 1})")
            actions.append("pool_empty")
            think(2, 4)
            continue

        # Pick a random asset to claim
        target = random.choice(pool)
        asset_id = target["asset_id"]
        seller_id = target["owner_id"]
        report_action("transfer_started", f"claiming {target['name']}")

        # Attempt to claim via coordinator (first-come-first-serve / race condition)
        try:
            r = coord_post("/claim", {
                "asset_id": asset_id,
                "pod": POD_NAME,
                "buyer_id": user_id,
            })
            result = r.json()

            if result.get("status") == "conflict":
                # Another agent beat us to it — race condition!
                print(f"[buyer] CONFLICT: {target['name']} already claimed")
                report_action("conflict_detected", f"{target['name']} — claimed by another agent")
                actions.append(f"conflict:{asset_id}")
                continue

            # Successfully claimed — now execute the actual transfer via backend API
            print(f"[buyer] claimed: {target['name']} — executing transfer")
            try:
                r = api_post(f"/api/assets/{asset_id}/transfer", {
                    "assetId": asset_id,
                    "newOwnerId": user_id,
                }, token=token)

                if r.status_code == 200:
                    report_action("transfer_completed", f"{target['name']} (${target.get('price', 0):.0f})")
                    coord_post("/transaction", {
                        "asset_id": asset_id,
                        "from": seller_id,
                        "to": user_id,
                        "status": "completed",
                    })
                    actions.append(f"transferred:{asset_id}")
                    print(f"[buyer] transfer SUCCESS: {target['name']}")
                else:
                    report_action("transfer_failed", f"{target['name']} — {r.status_code}")
                    coord_post("/transaction", {
                        "asset_id": asset_id,
                        "from": seller_id,
                        "to": user_id,
                        "status": "failed",
                    })
                    actions.append(f"transfer_failed:{asset_id}")
                    print(f"[buyer] transfer FAILED: {r.status_code} {r.text[:120]}")
            except Exception as e:
                report_action("transfer_failed", f"{target['name']} — {e}")
                actions.append(f"transfer_error:{asset_id}")

        except Exception as e:
            print(f"[buyer] claim error: {e}")
            actions.append("claim_error")

    return actions


def run_observer(token, user_id):
    """Observer: verify system consistency — asset counts, ownership."""
    actions = []

    think(2, 5)  # let sellers/buyers get ahead

    # Check asset list from the backend
    try:
        r = api_get("/api/assets?page=1&pageSize=100", token=token)
        if r.status_code == 200:
            data = r.json()
            total = data.get("totalCount", 0)
            items = data.get("items", [])

            # Count unique owners
            owners = set(a.get("userId", "") for a in items if a.get("userId"))
            report_action("consistency_check", f"{total} assets, {len(owners)} owners")
            actions.append(f"observed:{total}_assets_{len(owners)}_owners")

            # Cross-check with coordinator pool
            try:
                r2 = coord_get("/stats")
                stats = r2.json()
                coord_total = stats.get("transactions", {}).get("total", 0)
                conflicts = stats.get("transactions", {}).get("conflicts", 0)
                report_action("consistency_check",
                              f"txns:{coord_total} conflicts:{conflicts} pool:{stats.get('pool_size', 0)}")
                actions.append(f"verified:txns={coord_total},conflicts={conflicts}")
            except Exception:
                actions.append("coord_stats_error")

            print(f"[observer] system: {total} assets, {len(owners)} owners, "
                  f"coordinator has {coord_total} transactions")
        else:
            actions.append(f"observe_failed:{r.status_code}")
    except Exception as e:
        print(f"[observer] error: {e}")
        actions.append("observe_error")

    return actions


# ─── Main Agent Loop ─────────────────────────────────────────────────────────
def run_agent():
    start = time.time()
    all_actions = []

    # 1. Register and login
    token, user_id, username = register_and_login()
    if not token:
        return ["auth_failed"], int((time.time() - start) * 1000)
    all_actions.append("authenticated")

    think(0.5, 1)

    # 2. Register with coordinator and get role
    role = register_with_coordinator(user_id, username, token)
    all_actions.append(f"role:{role}")

    # 3. Execute role-specific scenario
    if role == "seller":
        actions = run_seller(token, user_id)
    elif role == "buyer":
        actions = run_buyer(token, user_id)
    elif role == "observer":
        actions = run_observer(token, user_id)
    else:
        actions = ["unknown_role"]

    all_actions.extend(actions)
    duration_ms = int((time.time() - start) * 1000)

    # Report a summary event so the dashboard shows what the agent accomplished
    tx_counts = {k: sum(1 for a in actions if a.startswith(k)) for k in ('transferred:', 'conflict:', 'created:')}
    summary_parts = [f"role:{role}"]
    if tx_counts['created:']:
        summary_parts.append(f"created {tx_counts['created:']} assets")
    if tx_counts['transferred:']:
        summary_parts.append(f"transferred {tx_counts['transferred:']} assets")
    if tx_counts['conflict:']:
        summary_parts.append(f"{tx_counts['conflict:']} conflicts")
    summary_parts.append(f"{duration_ms // 1000}s")
    report_action("agent_done", " · ".join(summary_parts))

    return all_actions, duration_ms


if __name__ == "__main__":
    try:
        actions, duration_ms = run_agent()
        status = "completed" if not any("error" in a or "failed" in a for a in actions) else "partial"
        send_result(status, actions, duration_ms)
        print(f"[agent] done in {duration_ms}ms: {actions}")
    except Exception as e:
        print(f"[agent] FATAL: {e}")
        send_result("error", [], 0, str(e))
