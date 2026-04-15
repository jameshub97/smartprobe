"""Reusable cleanup for hung local API listeners.

Used by cluster initialization and test preflight so both flows start from a
fresh local API state.
"""

from simulation_service_tool.menus.ports import (
    check_hung_dev_services,
    get_port_status,
    kill_port,
)


API_PORTS = ('5001', '5002', '5003')
_SIMULATION_SERVICE_PORT = '5002'


def clear_hung_api_listeners(restart_simulation: bool = False) -> dict:
    """Release hung local API listeners and optionally restart the sim service.

    Returns a structured result with status flags and a human-readable detail
    string for UI flows.
    """
    statuses = get_port_status()
    suspects = [
        suspect for suspect in check_hung_dev_services(statuses)
        if str(suspect.get('port')) in API_PORTS
    ]

    result = {
        'success': True,
        'detail': 'none detected',
        'released_ports': [],
        'failures': [],
        'restart_attempted': False,
        'restart_success': None,
        'restart_detail': None,
    }
    if not suspects:
        return result

    released_labels = []
    service_names = {
        port: statuses.get(port, {}).get('service', f'port {port}')
        for port in API_PORTS
    }

    for suspect in suspects:
        port = str(suspect.get('port'))
        label = f"{service_names.get(port, f'port {port}')} ({port})"
        kill_result = kill_port(port)

        if kill_result.get('error'):
            result['failures'].append(f"{label}: {kill_result['error']}")
            continue

        failed_pids = kill_result.get('failed_pids') or []
        if failed_pids:
            result['failures'].append(
                f"{label}: could not release PID(s) {', '.join(failed_pids)}"
            )
            continue

        if not kill_result.get('already_free'):
            result['released_ports'].append(port)
            released_labels.append(label)

    if restart_simulation and _SIMULATION_SERVICE_PORT in result['released_ports']:
        from simulation_service_tool.services.smart_diagnostics import _restart_service

        result['restart_attempted'] = True
        restart_success, restart_detail = _restart_service()
        result['restart_success'] = restart_success
        result['restart_detail'] = restart_detail

        if restart_success:
            released_labels.append(
                f"restarted {service_names[_SIMULATION_SERVICE_PORT]} ({_SIMULATION_SERVICE_PORT})"
            )
        else:
            result['failures'].append(
                f"{service_names[_SIMULATION_SERVICE_PORT]} ({_SIMULATION_SERVICE_PORT}): "
                f"restart failed ({restart_detail})"
            )

    detail_parts = []
    if released_labels:
        detail_parts.append(f"released {', '.join(released_labels)}")
    if result['failures']:
        detail_parts.append(f"failed {', '.join(result['failures'])}")

    result['success'] = not result['failures']
    result['detail'] = '; '.join(detail_parts) if detail_parts else 'none detected'
    return result