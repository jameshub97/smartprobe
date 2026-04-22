"""Cluster cleanup routes.

NOTE: In the current monolith (simulation_service.py) these routes are
registered on the *first* app instance (line ~70) which is then overwritten
by a second ``app = Flask(...)`` at line ~710. That means these endpoints are
effectively dead in the running process. This blueprint corrects that by
registering them on the single canonical app instance.
"""

import logging
from flask import Blueprint, jsonify, request

# Shared state — extracted from simulation_service.py during migration
# TODO: import from routes.state once shared state module is created
from simulation_service import (
    ClusterCleanup,
    is_valid_release_name,
    require_api_key,
    CLEANUP_OPERATIONS_TOTAL,
)

logger = logging.getLogger(__name__)

cleanup_bp = Blueprint('cleanup', __name__)

# Module-level handler — mirrors existing cleanup_handler singleton
_cleanup_handler = ClusterCleanup()


@cleanup_bp.route('/api/cleanup/all', methods=['POST'])
@require_api_key
def cleanup_all():
    CLEANUP_OPERATIONS_TOTAL.labels(resource_type='all', result='attempted').inc()
    data = request.json or {}
    dry_run = data.get('dry_run', False)
    results = _cleanup_handler.cleanup_all(dry_run=dry_run)
    return jsonify(results)


@cleanup_bp.route('/api/cleanup/release/<release_name>', methods=['DELETE'])
@require_api_key
def cleanup_release(release_name):
    if not is_valid_release_name(release_name):
        return jsonify({'error': 'Invalid release name'}), 400
    CLEANUP_OPERATIONS_TOTAL.labels(resource_type='release', result='attempted').inc()
    data = request.json or {}
    dry_run = data.get('dry_run', False)
    results = _cleanup_handler.cleanup_specific_release(release_name, dry_run=dry_run)
    return jsonify(results)


@cleanup_bp.route('/api/cleanup/stuck', methods=['POST'])
@require_api_key
def cleanup_stuck():
    CLEANUP_OPERATIONS_TOTAL.labels(resource_type='stuck', result='attempted').inc()
    data = request.json or {}
    dry_run = data.get('dry_run', False)
    results = {
        'pvcs': _cleanup_handler.cleanup_orphaned_pvcs(dry_run),
        'pdbs': _cleanup_handler.cleanup_pdbs(dry_run),
    }
    return jsonify(results)


@cleanup_bp.route('/api/cleanup/reset', methods=['POST'])
@require_api_key
def reset_cluster():
    CLEANUP_OPERATIONS_TOTAL.labels(resource_type='reset', result='attempted').inc()
    data = request.json or {}
    dry_run = data.get('dry_run', False)
    results = _cleanup_handler.reset_cluster_state(dry_run=dry_run)
    return jsonify(results)


@cleanup_bp.route('/api/cleanup/verify', methods=['GET'])
def verify_clean_state():
    results = _cleanup_handler.verify_clean_state()
    return jsonify(results)


@cleanup_bp.route('/api/cleanup/preflight', methods=['GET'])
def cleanup_preflight():
    state = _cleanup_handler.verify_clean_state()
    if state['is_clean']:
        return jsonify({
            'ready': True,
            'message': 'Cluster is clean and ready for tests',
            'state': state,
        })

    issues = []
    if state['helm_test_releases'] > 0:
        issues.append(f"{state['helm_test_releases']} test releases exist")
    if state['playwright_pods'] > 0:
        issues.append(f"{state['playwright_pods']} playwright pods exist")
    if state['playwright_pvcs'] > 0:
        issues.append(f"{state['playwright_pvcs']} PVCs exist")
    if state['conflicting_pdbs'] > 0:
        issues.append("Conflicting PDB exists")
    return jsonify({
        'ready': False,
        'message': 'Cluster has leftover resources',
        'issues': issues,
        'state': state,
        'suggested_action': 'POST to /api/cleanup/reset',
    })
