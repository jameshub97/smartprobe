"""Coordinator Service — port 5003

Manages inter-agent coordination state independently of the simulation monitor:
  - Agent registration and role assignment (seller / buyer / observer)
  - Shared asset pool (sellers publish, buyers discover)
  - First-come-first-serve claim arbitration (intentional race conditions)
  - Transaction ledger and stats

Runs as a separate process so:
  - State survives simulation_service restarts
  - Claim locks don't contend with kubectl subprocess threads
  - Lifecycle is tied to simulation start/stop, not pod monitoring
"""

import asyncio
import time
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

logging.basicConfig(level=logging.WARNING)

# ─── State ───────────────────────────────────────────────────────────────────
_lock = asyncio.Lock()
_state = {
    'agents': {},        # pod_name -> {role, user_id, username, token, registered_at}
    'asset_pool': [],    # [{asset_id, owner_id, name, price, created_at}]
    'transactions': [],  # [{asset_id, from, to, status, timestamp}]
    'round': 0,
}

MAX_TRANSACTIONS = 5000


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.post('/api/coordinator/register')
async def register(request: Request):
    """Agent registers and receives a role assignment."""
    data = await request.json()
    pod = data.get('pod', 'unknown')
    user_id = data.get('user_id', '')
    username = data.get('username', '')
    token = data.get('token', '')

    async with _lock:
        agent_count = len(_state['agents'])
        # 40% sellers, 50% buyers, 10% observers
        r = agent_count % 10
        if r < 4:
            role = 'seller'
        elif r < 9:
            role = 'buyer'
        else:
            role = 'observer'

        _state['agents'][pod] = {
            'role': role,
            'user_id': user_id,
            'username': username,
            'token': token,
            'registered_at': time.time(),
        }
        pool_size = len(_state['asset_pool'])
        tx_count = len(_state['transactions'])

    return {
        'role': role,
        'agent_count': agent_count + 1,
        'pool_size': pool_size,
        'tx_count': tx_count,
    }


@app.get('/api/coordinator/agents')
async def list_agents():
    async with _lock:
        agents = [
            {'pod': pod, 'role': info['role'], 'user_id': info['user_id'], 'username': info['username']}
            for pod, info in _state['agents'].items()
        ]
    return {'agents': agents, 'count': len(agents)}


@app.get('/api/coordinator/assets')
async def list_assets(exclude_owner: str = ''):
    """Return available assets, optionally excluding a given owner."""
    async with _lock:
        pool = [a for a in _state['asset_pool'] if a.get('owner_id') != exclude_owner]
        total = len(_state['asset_pool'])
    return {'assets': pool, 'total': total}


@app.post('/api/coordinator/assets')
async def add_asset(request: Request):
    """Seller reports a newly created asset to the shared pool."""
    data = await request.json()
    async with _lock:
        _state['asset_pool'].append({
            'asset_id': data.get('asset_id'),
            'owner_id': data.get('owner_id'),
            'name': data.get('name'),
            'price': data.get('price'),
            'created_at': time.time(),
        })
        pool_size = len(_state['asset_pool'])
    return {'status': 'ok', 'pool_size': pool_size}


@app.post('/api/coordinator/claim')
async def claim_asset(request: Request):
    """First-come-first-serve asset claim. Returns 'claimed' or 'conflict'."""
    data = await request.json()
    asset_id = data.get('asset_id')
    buyer_pod = data.get('pod')
    buyer_id = data.get('buyer_id')

    async with _lock:
        asset = next((a for a in _state['asset_pool'] if a['asset_id'] == asset_id), None)

        if asset is None:
            _state['transactions'].append({
                'asset_id': asset_id,
                'buyer': buyer_pod,
                'buyer_id': buyer_id,
                'status': 'conflict',
                'timestamp': time.time(),
            })
            _trim_transactions()
            return {'status': 'conflict', 'message': 'Asset already claimed by another agent'}

        _state['asset_pool'] = [a for a in _state['asset_pool'] if a['asset_id'] != asset_id]
        _state['transactions'].append({
            'asset_id': asset_id,
            'seller_id': asset['owner_id'],
            'buyer': buyer_pod,
            'buyer_id': buyer_id,
            'status': 'claimed',
            'timestamp': time.time(),
        })
        _trim_transactions()

    return {'status': 'claimed', 'asset': asset}


@app.post('/api/coordinator/transaction')
async def record_transaction(request: Request):
    """Record a completed or failed transfer outcome."""
    data = await request.json()
    async with _lock:
        _state['transactions'].append({
            'asset_id': data.get('asset_id'),
            'from': data.get('from'),
            'to': data.get('to'),
            'status': data.get('status', 'completed'),
            'timestamp': time.time(),
        })
        _trim_transactions()
    return {'status': 'ok'}


@app.get('/api/coordinator/stats')
async def stats():
    """Summary stats for the dashboard."""
    async with _lock:
        txs = _state['transactions']
        agents = _state['agents']
        return {
            'agents': len(agents),
            'roles': {
                'sellers': sum(1 for a in agents.values() if a['role'] == 'seller'),
                'buyers': sum(1 for a in agents.values() if a['role'] == 'buyer'),
                'observers': sum(1 for a in agents.values() if a['role'] == 'observer'),
            },
            'pool_size': len(_state['asset_pool']),
            'transactions': {
                'total': len(txs),
                'completed': sum(1 for t in txs if t['status'] == 'completed'),
                'conflicts': sum(1 for t in txs if t['status'] == 'conflict'),
                'failed': sum(1 for t in txs if t['status'] == 'failed'),
                'claimed': sum(1 for t in txs if t['status'] == 'claimed'),
            },
        }


@app.post('/api/coordinator/reset')
async def reset():
    """Reset all state — called at simulation start."""
    async with _lock:
        _state['agents'].clear()
        _state['asset_pool'].clear()
        _state['transactions'].clear()
        _state['round'] = 0
    return {'status': 'reset'}


@app.get('/api/coordinator/health')
async def health():
    return {'status': 'ok'}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _trim_transactions():
    """Keep transaction list bounded — called inside _lock."""
    if len(_state['transactions']) > MAX_TRANSACTIONS:
        del _state['transactions'][:len(_state['transactions']) - MAX_TRANSACTIONS]


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    import uvicorn
    port = 5003
    for arg in sys.argv[1:]:
        if arg.startswith('--port='):
            port = int(arg.split('=')[1])
    print(f'[coordinator] starting on port {port}')
    uvicorn.run(app, host='0.0.0.0', port=port)
