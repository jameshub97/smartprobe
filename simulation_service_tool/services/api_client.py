"""API client for the simulation service."""

import os
import requests

from simulation_service_tool.ui.styles import SERVICE_URL

API_KEY = os.environ.get('SIMULATION_API_KEY', 'dev-key-change-in-production')


def _auth_headers():
    """Return authorization headers for API requests."""
    return {'Authorization': f'Bearer {API_KEY}'}


def check_service():
    """Check if simulation service is running."""
    try:
        response = requests.get(f"{SERVICE_URL}/health", timeout=2)
        return response.status_code == 200
    except Exception:
        return False


def call_service(endpoint, method='GET', data=None):
    """Call the simulation service API."""
    url = f"{SERVICE_URL}{endpoint}"
    try:
        method = method.upper()
        if method == 'GET':
            response = requests.get(url, headers=_auth_headers(), timeout=10)
        elif method == 'DELETE':
            response = requests.delete(url, json=data, headers=_auth_headers(), timeout=10)
        else:
            response = requests.post(url, json=data, headers=_auth_headers(), timeout=10)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 401:
            return {'error': 'Unauthorized. Check SIMULATION_API_KEY environment variable.'}
        else:
            return {'error': response.text}
    except requests.exceptions.ConnectionError:
        return {'error': f'Could not connect to simulation service at {SERVICE_URL}. Start the service or use direct mode.'}
    except Exception as e:
        return {'error': str(e)}
