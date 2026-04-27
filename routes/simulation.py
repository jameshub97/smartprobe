"""Core simulation routes: health, status, agent telemetry, start/stop/list."""

import logging
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

# Shared state — extracted from simulation_service.py during migration.
# TODO: import from routes.state once shared state module is created.
from simulation_service import (
    K8sSimulationMonitor,
    TestController,
    PRESETS,
    SIMULATION_MODES,
    is_valid_release_name,
    is_valid_persona,
    require_api_key,
    get_status,
    add_activity_log,
    run_cli_command,
    _activity_log,
    _agent_states,
    _agent_results,
    _event_totals,
    _cache,
    _read_prometheus_gauges,
    _enrich_summary_with_prometheus,
    _coordinator_stats_safe,
    _coordinator_reset_safe,
    MAX_LOG_ENTRIES,
    MAX_AGENT_RESULTS,
    AGENT_ORCHESTRATION_TOTAL,
    AGENT_TEST_DURATION,
    AGENT_PODS_ACTIVE,
    AGENT_PODS_SUCCEEDED,
    AGENT_PODS_FAILED,
    AGENT_PODS_PENDING,
    KUEUE_ACTIVE,
    KUEUE_PENDING_WORKLOADS,
    KUEUE_ADMITTED_WORKLOADS,
    COORDINATOR_URL,
)

logger = logging.getLogger(__name__)

simulation_bp = Blueprint('simulation', __name__)


@simulation_bp.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'time': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    })


@simulation_bp.route('/api/simulation/summary')
def simulation_summary():
    now = time.time()
    if _cache['data'] and _cache['timestamp'] and now - _cache['timestamp'] < _cache['ttl']:
        data = _cache['data']
    else:
        data = k8s_monitor.get_detailed_summary()
        _cache['data'] = data
        _cache['timestamp'] = now

    AGENT_PODS_ACTIVE.set(data.get('running', 0))
    AGENT_PODS_SUCCEEDED.set(data.get('success', 0))
    AGENT_PODS_FAILED.set(data.get('errors', 0))
    AGENT_PODS_PENDING.set(data.get('pending', 0))

    try:
        from simulation_service_tool.services.kueue import (
            is_kueue_installed, get_cluster_queue_status, get_local_queue_status,
        )
        kueue_installed = is_kueue_installed()
        KUEUE_ACTIVE.set(1 if kueue_installed else 0)
        if kueue_installed:
            cq = get_cluster_queue_status()
            lq = get_local_queue_status()
            pending_wl = cq.get('pending_workloads', 0) if cq.get('exists') else 0
            admitted_wl = cq.get('admitted_workloads', 0) if cq.get('exists') else 0
            KUEUE_PENDING_WORKLOADS.set(pending_wl)
            KUEUE_ADMITTED_WORKLOADS.set(admitted_wl)
            avg_duration = data.get('avg_duration', 7)
            throughput_per_cycle = max(admitted_wl, 1)
            estimated_drain_seconds = (pending_wl / throughput_per_cycle) * avg_duration
            data['kueue'] = {
                'active': True,
                'cluster_queue': cq,
                'local_queue': lq,
                'pending_workloads': pending_wl,
                'admitted_workloads': admitted_wl,
                'estimated_drain_seconds': round(estimated_drain_seconds, 1),
            }
        else:
            KUEUE_PENDING_WORKLOADS.set(0)
            KUEUE_ADMITTED_WORKLOADS.set(0)
            data['kueue'] = {'active': False}
    except Exception:
        data['kueue'] = {'active': False}

    data['prometheus'] = _read_prometheus_gauges()
    _enrich_summary_with_prometheus(data, data['prometheus'])
    data['coordinator'] = _coordinator_stats_safe()
    return jsonify(data)


@simulation_bp.route('/api/simulation/activity')
def simulation_activity():
    limit = request.args.get('limit', 10, type=int)
    limit = max(1, min(limit, MAX_LOG_ENTRIES))
    cached = _cache.get('data') or {}
    return jsonify({
        'activity': _activity_log[-limit:],
        'summary': {
            'sleeping': cached.get('success', 0),
            'pending': cached.get('pending', 0),
            'running': cached.get('running', 0),
        },
        'totals': dict(_event_totals),
    })


@simulation_bp.route('/api/simulation/agent-action', methods=['POST'])
def agent_action():
    data = request.get_json(force=True)
    pod = data.get('pod', 'unknown')
    action = data.get('action', '')
    details = data.get('details')
    ACTION_TYPES = {
        'browsing': 'browsing', 'registered': 'registered', 'logged_in': 'logged_in',
        'asset_created': 'asset_created', 'asset_listed': 'asset_listed',
        'transfer_started': 'transfer_started', 'transfer_completed': 'transfer_completed',
        'transfer_failed': 'transfer_failed', 'conflict_detected': 'conflict_detected',
        'consistency_check': 'consistency_check', 'agent_done': 'agent_done',
    }
    event_type = ACTION_TYPES.get(action, 'action')
    add_activity_log(event_type, pod, details)
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
    state = _agent_states.get(pod, {'username': None, 'history': []})
    if event_type in ('registered', 'logged_in') and details:
        state['username'] = details
    state.update({'timestamp': ts, 'type': event_type, 'details': details})
    history = state.setdefault('history', [])
    history.append({'ts': ts, 'type': event_type, 'details': details})
    if len(history) > 20:
        del history[:len(history) - 20]
    _agent_states[pod] = state
    return jsonify({'status': 'ok'})


@simulation_bp.route('/api/simulation/agent-states')
def get_agent_states():
    return jsonify({'states': _agent_states})


@simulation_bp.route('/api/simulation/live-logs')
def live_logs():
    tail = request.args.get('tail', '8')
    try:
        tail = min(int(tail), 50)
    except (ValueError, TypeError):
        tail = 8
    list_result = run_cli_command([
        'kubectl', 'get', 'pods', '-l', 'app=playwright-agent',
        '--field-selector', 'status.phase=Running',
        '--no-headers', '-o', 'custom-columns=NAME:.metadata.name',
    ])
    pods = [p.strip() for p in (list_result.stdout or '').splitlines() if p.strip()][:10]
    if not pods:
        return jsonify({'lines': [], 'pods': 0})
    lines = []
    for pod in pods:
        result = run_cli_command(['kubectl', 'logs', pod, f'--tail={tail}'])
        if result.returncode == 0 and result.stdout:
            for line in result.stdout.splitlines():
                if line.strip():
                    lines.append({'pod': pod, 'line': line})
    return jsonify({'lines': lines, 'pods': len(pods)})


@simulation_bp.route('/api/simulation/agent-result', methods=['POST'])
def agent_result():
    data = request.get_json(force=True)
    _agent_results.append({
        'timestamp': datetime.now(timezone.utc).strftime('%H:%M:%S'),
        'pod': data.get('pod', 'unknown'),
        'persona': data.get('persona', 'unknown'),
        'status': data.get('status', 'unknown'),
        'actions': data.get('actions', []),
        'durationMs': data.get('durationMs', 0),
        'error': data.get('error'),
    })
    if len(_agent_results) > MAX_AGENT_RESULTS:
        del _agent_results[:len(_agent_results) - MAX_AGENT_RESULTS]
    return jsonify({'status': 'ok'})


@simulation_bp.route('/api/simulation/agent-results')
def get_agent_results():
    limit = request.args.get('limit', 100, type=int)
    limit = max(1, min(limit, MAX_AGENT_RESULTS))
    return jsonify({'results': _agent_results[-limit:]})


@simulation_bp.route('/api/simulation/agent-detail/<pod_name>')
def get_agent_detail(pod_name):
    from simulation_service_tool.services.command_runner import is_valid_k8s_name
    if not is_valid_k8s_name(pod_name):
        return jsonify({'error': 'Invalid pod name'}), 400
    state = _agent_states.get(pod_name, {})
    result = next((r for r in reversed(_agent_results) if r.get('pod') == pod_name), None)
    role = None
    try:
        import requests as _req
        r = _req.get(f'{COORDINATOR_URL}/agents', timeout=1)
        if r.ok:
            agents = r.json().get('agents', [])
            match = next((a for a in agents if a.get('pod') == pod_name), None)
            if match:
                role = match.get('role')
    except Exception:
        pass
    return jsonify({'pod': pod_name, 'state': state, 'result': result, 'role': role})


@simulation_bp.route('/api/simulation/pod-logs/<pod_name>', methods=['GET'])
def get_pod_logs(pod_name):
    from simulation_service_tool.services.command_runner import is_valid_k8s_name
    if not is_valid_k8s_name(pod_name):
        return jsonify({'error': 'Invalid pod name'}), 400
    tail = request.args.get('tail', '300')
    try:
        tail = min(int(tail), 1000)
    except (ValueError, TypeError):
        tail = 300
    result = run_cli_command(["kubectl", "logs", pod_name, f"--tail={tail}"])
    if result.returncode != 0:
        return jsonify({'pod': pod_name, 'logs': result.stderr or 'No logs available', 'error': True})
    return jsonify({'pod': pod_name, 'logs': result.stdout or '(no output yet)'})


@simulation_bp.route('/api/simulation/start', methods=['POST'])
@require_api_key
def start_simulation():
    req = request.get_json(force=True)
    name = req.get('name', f"test-{int(time.time())}")
    completions = int(req.get('completions', 10))
    parallelism = int(req.get('parallelism', 5))
    persona = req.get('persona', 'impatient')
    workers = int(req.get('workers', 1))
    wait = bool(req.get('wait', False))
    skip_preflight = bool(req.get('skip_preflight', False))
    image_repository = (req.get('imageRepository') or '').strip() or None
    image_tag = (req.get('imageTag') or '').strip() or None
    replica_count = req.get('replicaCount')
    shard_total = req.get('shardTotal')
    request_memory = (req.get('requestMemory') or '').strip() or None
    request_cpu = (req.get('requestCpu') or '').strip() or None
    limit_memory = (req.get('limitMemory') or '').strip() or None
    limit_cpu = (req.get('limitCpu') or '').strip() or None
    backoff_limit = req.get('backoffLimit')
    ttl_seconds_after_finished = req.get('ttlSecondsAfterFinished')
    command_override = (req.get('commandOverride') or '').strip() or None
    kueue = req.get('kueue')
    mode = (req.get('mode') or '').strip() or 'basic'

    if mode not in SIMULATION_MODES:
        return jsonify({'success': False, 'error': f'Invalid mode. Must be one of: {SIMULATION_MODES}'}), 400

    _coordinator_reset_safe()

    if not image_repository:
        image_repository = 'playwright-agent'
    if not image_tag:
        image_tag = 'latest'
    if not command_override:
        command_override = 'python3 /app/run.py'
    if ttl_seconds_after_finished is None:
        ttl_seconds_after_finished = 3600
    if not request_memory:
        request_memory = '64Mi'
    if not request_cpu:
        request_cpu = '50m'
    if not limit_memory:
        limit_memory = '128Mi'
    if not limit_cpu:
        limit_cpu = '100m'

    replica_count = int(replica_count) if replica_count is not None else None
    shard_total = int(shard_total) if shard_total is not None else None
    backoff_limit = int(backoff_limit) if backoff_limit is not None else None
    ttl_seconds_after_finished = int(ttl_seconds_after_finished) if ttl_seconds_after_finished is not None else None

    if not is_valid_release_name(name):
        return jsonify({'success': False, 'error': 'Invalid release name. Use lowercase alphanumeric and hyphens only.'}), 400
    if not is_valid_persona(persona):
        return jsonify({'success': False, 'error': 'Invalid persona name.'}), 400
    if not (1 <= parallelism <= 200):
        return jsonify({'success': False, 'error': 'Parallelism must be between 1 and 200.'}), 400
    if not (1 <= completions <= 5000):
        return jsonify({'success': False, 'error': 'Completions must be between 1 and 5000.'}), 400
    if replica_count is not None and not (1 <= replica_count <= 5000):
        return jsonify({'success': False, 'error': 'Replica count must be between 1 and 5000.'}), 400
    if shard_total is not None and not (1 <= shard_total <= 5000):
        return jsonify({'success': False, 'error': 'Shard total must be between 1 and 5000.'}), 400
    if backoff_limit is not None and backoff_limit < 0:
        return jsonify({'success': False, 'error': 'Backoff limit must be zero or positive.'}), 400
    if ttl_seconds_after_finished is not None and ttl_seconds_after_finished < 0:
        return jsonify({'success': False, 'error': 'TTL seconds after finished must be zero or positive.'}), 400

    if not skip_preflight:
        preflight = K8sSimulationMonitor.preflight_check()
        if preflight['has_conflicts']:
            return jsonify({
                'success': False,
                'error': 'Resource conflicts detected',
                'conflicts': preflight['conflicts'],
                'suggestion': 'Run cleanup first or pass skip_preflight=true to override',
            }), 409

    _test_t0 = time.time()
    result = TestController.run_test(
        name, completions, parallelism, persona, workers, wait,
        image_repository=image_repository,
        image_tag=image_tag,
        replica_count=replica_count,
        shard_total=shard_total,
        request_memory=request_memory,
        request_cpu=request_cpu,
        limit_memory=limit_memory,
        limit_cpu=limit_cpu,
        backoff_limit=backoff_limit,
        ttl_seconds_after_finished=ttl_seconds_after_finished,
        command_override=command_override,
        kueue=kueue,
    )
    _test_duration = time.time() - _test_t0
    action_result = 'success' if result.get('success') else 'failure'
    AGENT_ORCHESTRATION_TOTAL.labels(action='start_test', result=action_result).inc()
    _preset = name.rsplit('-', 1)[0] if '-' in name else 'custom'
    if _preset not in PRESETS:
        _preset = 'custom'
    AGENT_TEST_DURATION.labels(preset=_preset, persona=persona).observe(_test_duration)
    return jsonify(result)


@simulation_bp.route('/api/simulation/stop', methods=['POST'])
@require_api_key
def stop_simulation():
    req = request.get_json(force=True)
    name = req.get('name')
    if not name:
        return jsonify({'success': False, 'error': 'Missing test name'}), 400
    if not is_valid_release_name(name):
        return jsonify({'success': False, 'error': 'Invalid release name.'}), 400
    result = TestController.stop_test(name)
    action_result = 'success' if result.get('success') else 'failure'
    AGENT_ORCHESTRATION_TOTAL.labels(action='stop_test', result=action_result).inc()
    return jsonify(result)


@simulation_bp.route('/api/simulation/tests')
def list_tests():
    return jsonify(TestController.list_tests())


@simulation_bp.route('/api/simulation/presets')
def list_presets():
    return jsonify(PRESETS)


@simulation_bp.route('/api/simulation/status')
def simulation_status():
    return jsonify(get_status())
