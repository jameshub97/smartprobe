"""Tests for the coordinator service /api/coordinator/agents endpoint.

Covers:
  - GET /api/coordinator/agents returns correct shape when empty and populated
  - POST /api/coordinator/register adds an agent and returns a role
  - Role assignment follows the 40/50/10 (seller/buyer/observer) distribution
  - Re-registering the same pod keeps the original role (idempotency)
  - POST /api/coordinator/reset clears all agents
  - Token is never leaked in the agents list
  - Agent count tracks correctly across registrations and reset
"""

import pytest
from starlette.testclient import TestClient

# Import the FastAPI app from the root-level coordinator service.
# Use sys.path manipulation so this works regardless of working directory.
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import coordinator_service as coord_module
from coordinator_service import app, _state


@pytest.fixture(autouse=True)
def reset_state():
    """Reset coordinator in-memory state before every test."""
    _state['agents'].clear()
    _state['asset_pool'].clear()
    _state['transactions'].clear()
    _state['round'] = 0
    yield
    _state['agents'].clear()
    _state['asset_pool'].clear()
    _state['transactions'].clear()
    _state['round'] = 0


@pytest.fixture()
def client():
    return TestClient(app)


def _register(client, pod, user_id='u1', username='alice', token='tok'):
    return client.post('/api/coordinator/register', json={
        'pod': pod, 'user_id': user_id, 'username': username, 'token': token,
    })


# ─── GET /api/coordinator/agents ──────────────────────────────────────────────

class TestListAgents:
    def test_empty_on_startup(self, client):
        r = client.get('/api/coordinator/agents')
        assert r.status_code == 200
        data = r.json()
        assert data['agents'] == []
        assert data['count'] == 0

    def test_returns_agent_after_registration(self, client):
        _register(client, 'pod-1', user_id='u1', username='alice')
        r = client.get('/api/coordinator/agents')
        assert r.status_code == 200
        data = r.json()
        assert data['count'] == 1
        assert len(data['agents']) == 1
        agent = data['agents'][0]
        assert agent['pod'] == 'pod-1'
        assert agent['username'] == 'alice'
        assert agent['user_id'] == 'u1'
        assert 'role' in agent

    def test_token_not_in_agent_list(self, client):
        """Tokens must never be exposed in the agents list."""
        _register(client, 'pod-1', token='secret-jwt-token')
        data = client.get('/api/coordinator/agents').json()
        agent = data['agents'][0]
        assert 'token' not in agent

    def test_multiple_agents_all_listed(self, client):
        for i in range(5):
            _register(client, f'pod-{i}', user_id=f'u{i}', username=f'user{i}')
        data = client.get('/api/coordinator/agents').json()
        assert data['count'] == 5
        pods = {a['pod'] for a in data['agents']}
        assert pods == {f'pod-{i}' for i in range(5)}

    def test_count_matches_agents_length(self, client):
        for i in range(3):
            _register(client, f'pod-{i}')
        data = client.get('/api/coordinator/agents').json()
        assert data['count'] == len(data['agents'])


# ─── POST /api/coordinator/register ──────────────────────────────────────────

class TestRegister:
    def test_returns_role(self, client):
        r = _register(client, 'pod-0')
        assert r.status_code == 200
        data = r.json()
        assert data['role'] in ('seller', 'buyer', 'observer')

    def test_returns_agent_count(self, client):
        r = _register(client, 'pod-0')
        assert r.json()['agent_count'] == 1
        r2 = _register(client, 'pod-1')
        assert r2.json()['agent_count'] == 2

    def test_role_assignment_40_50_10(self, client):
        """First 10 registrations follow seller(4) / buyer(5) / observer(1)."""
        roles = []
        for i in range(10):
            r = _register(client, f'pod-{i}', user_id=f'u{i}', username=f'user{i}')
            roles.append(r.json()['role'])
        assert roles.count('seller') == 4
        assert roles.count('buyer') == 5
        assert roles.count('observer') == 1

    def test_first_agent_is_seller(self, client):
        r = _register(client, 'pod-0')
        assert r.json()['role'] == 'seller'

    def test_fifth_agent_is_buyer(self, client):
        for i in range(4):
            _register(client, f'pod-{i}', user_id=f'u{i}', username=f'user{i}')
        r = _register(client, 'pod-4', user_id='u4', username='user4')
        assert r.json()['role'] == 'buyer'

    def test_tenth_agent_is_observer(self, client):
        for i in range(9):
            _register(client, f'pod-{i}', user_id=f'u{i}', username=f'user{i}')
        r = _register(client, 'pod-9', user_id='u9', username='user9')
        assert r.json()['role'] == 'observer'

    def test_reregistration_keeps_original_role(self, client):
        """Re-registering an existing pod must not change its role."""
        first = _register(client, 'pod-0', user_id='u1', username='alice')
        original_role = first.json()['role']
        # Register 5 more agents so count changes significantly
        for i in range(1, 6):
            _register(client, f'pod-{i}', user_id=f'u{i}', username=f'user{i}')
        # Re-register the original pod
        second = _register(client, 'pod-0', user_id='u1', username='alice')
        assert second.json()['role'] == original_role

    def test_reregistration_does_not_duplicate_agent(self, client):
        """Re-registering the same pod should not add a duplicate."""
        _register(client, 'pod-0')
        _register(client, 'pod-0')
        data = client.get('/api/coordinator/agents').json()
        assert data['count'] == 1

    def test_registration_updates_username(self, client):
        """Re-registration with a new username should update the stored value."""
        _register(client, 'pod-0', username='alice')
        _register(client, 'pod-0', username='alice-updated')
        data = client.get('/api/coordinator/agents').json()
        assert data['agents'][0]['username'] == 'alice-updated'


# ─── POST /api/coordinator/reset ─────────────────────────────────────────────

class TestReset:
    def test_reset_clears_agents(self, client):
        for i in range(3):
            _register(client, f'pod-{i}')
        client.post('/api/coordinator/reset')
        data = client.get('/api/coordinator/agents').json()
        assert data['agents'] == []
        assert data['count'] == 0

    def test_registration_after_reset_restarts_role_cycle(self, client):
        """After reset, role assignment restarts from position 0 (seller)."""
        for i in range(5):
            _register(client, f'pod-{i}', user_id=f'u{i}', username=f'user{i}')
        client.post('/api/coordinator/reset')
        r = _register(client, 'pod-new', user_id='u99', username='new-agent')
        assert r.json()['role'] == 'seller'

    def test_reset_returns_ok(self, client):
        r = client.post('/api/coordinator/reset')
        assert r.status_code == 200
        assert r.json()['status'] == 'reset'
