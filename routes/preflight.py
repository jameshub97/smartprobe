"""Preflight check route."""

import logging
from flask import Blueprint, jsonify

# Shared state — extracted from simulation_service.py during migration
# TODO: import from routes.state once shared state module is created
from simulation_service import K8sSimulationMonitor

logger = logging.getLogger(__name__)

preflight_bp = Blueprint('preflight', __name__)


@preflight_bp.route('/api/preflight', methods=['GET'])
def preflight_check_endpoint():
    """Check for conflicts before deployment."""
    try:
        result = K8sSimulationMonitor.preflight_check()
        return jsonify(result)
    except Exception as exc:
        logger.exception("preflight_check failed")
        return jsonify({'error': str(exc), 'has_conflicts': False, 'conflicts': []}), 500
