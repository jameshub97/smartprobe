"""Diagnostics routes: deployment, performance, network, job progress, cluster, cost."""

import logging
from flask import Blueprint, jsonify

# Shared state — extracted from simulation_service.py during migration.
# TODO: import from routes.state once shared state module is created.
from simulation_service import (
    DeploymentDiagnostics,
    PerformanceDiagnostics,
    NetworkDiagnostics,
    JobProgressDiagnostics,
    ClusterHealthDiagnostics,
    CostDiagnostics,
    DIAGNOSTICS_TRIGGERED_TOTAL,
)

logger = logging.getLogger(__name__)

diagnostics_bp = Blueprint('diagnostics', __name__)


@diagnostics_bp.route('/api/diagnostics/deployment/<release>', methods=['GET'])
def diagnose_deployment(release):
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='deployment').inc()
    diag = DeploymentDiagnostics()
    return jsonify(diag.diagnose_deployment_failure(release))


@diagnostics_bp.route('/api/diagnostics/performance/<release>', methods=['GET'])
def diagnose_performance(release):
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='performance').inc()
    diag = PerformanceDiagnostics()
    return jsonify(diag.analyze_pod_performance(release))


@diagnostics_bp.route('/api/diagnostics/network/<release>', methods=['GET'])
def diagnose_network(release):
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='network').inc()
    diag = NetworkDiagnostics()
    return jsonify(diag.diagnose_connectivity(release))


@diagnostics_bp.route('/api/diagnostics/progress/<release>', methods=['GET'])
def diagnose_progress(release):
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='progress').inc()
    diag = JobProgressDiagnostics()
    return jsonify(diag.analyze_job_progress(release))


@diagnostics_bp.route('/api/diagnostics/cluster', methods=['GET'])
def diagnose_cluster():
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='cluster').inc()
    diag = ClusterHealthDiagnostics()
    return jsonify(diag.cluster_health_check())


@diagnostics_bp.route('/api/diagnostics/cost/<release>', methods=['GET'])
def diagnose_cost(release):
    DIAGNOSTICS_TRIGGERED_TOTAL.labels(diagnostic_type='cost').inc()
    diag = CostDiagnostics()
    return jsonify(diag.estimate_test_cost(release))
