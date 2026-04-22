"""Port management helpers and interactive menu."""

import os
import signal
import subprocess
import time
import urllib.error
import urllib.request

try:
    import questionary
except ImportError:  # pragma: no cover - optional interactive dependency
    questionary = None

from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen
from simulation_service_tool.cli.prompts import _prompt_go_back


DEV_PORTS = {
    '3000': 'Frontend (Vite)',
    '3001': 'Frontend (alternate)',
    '5001': 'Backend API (.NET)',
    '5002': 'Simulation Service (Flask)',
    '5432': 'PostgreSQL',
    '8080': 'NGINX',
}

DEV_PORT_PROBES = {
    '3000': {'url': 'http://127.0.0.1:3000/', 'expect_status': None},
    '3001': {'url': 'http://127.0.0.1:3001/', 'expect_status': None},
    '5001': {'url': 'http://127.0.0.1:5001/', 'expect_status': None},
    '5002': {'url': 'http://127.0.0.1:5002/health', 'expect_status': 200},
    '8080': {'url': 'http://127.0.0.1:8080/', 'expect_status': None},
}


def _normalize_port(port) -> str:
    return str(port).strip()


def _parse_lsof_output(stdout: str):
    processes = []
    lines = [line for line in stdout.splitlines() if line.strip()]
    for line in lines[1:]:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        processes.append({
            'command': parts[0],
            'pid': parts[1],
            'user': parts[2],
            'name': parts[8],
        })
    return processes


def get_port_status(port: str = None):
    """Return status for one port or all tracked development ports."""
    if port is None:
        return {tracked_port: get_port_status(tracked_port) for tracked_port in DEV_PORTS}

    port = _normalize_port(port)
    if not port.isdigit():
        return {
            'port': port,
            'service': DEV_PORTS.get(port, 'Unknown'),
            'in_use': False,
            'processes': [],
            'error': f"Invalid port: {port}",
        }

    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return {
            'port': port,
            'service': DEV_PORTS.get(port, 'Unknown'),
            'in_use': False,
            'processes': [],
            'error': "'lsof' is not available on PATH.",
        }

    processes = _parse_lsof_output(result.stdout)
    return {
        'port': port,
        'service': DEV_PORTS.get(port, 'Unknown'),
        'in_use': bool(processes),
        'processes': processes,
        'error': None if result.returncode in (0, 1) else (result.stderr.strip() or 'Failed to inspect port'),
    }


def kill_port(port: str):
    """Terminate listeners on a port, preferring SIGTERM before SIGKILL."""
    port = _normalize_port(port)
    status = get_port_status(port)
    result = {
        'port': port,
        'service': status.get('service', 'Unknown'),
        'killed_pids': [],
        'failed_pids': [],
        'already_free': False,
        'error': status.get('error'),
    }

    if result['error']:
        return result

    if not status.get('in_use'):
        result['already_free'] = True
        return result

    target_pids = []
    for process in status.get('processes', []):
        try:
            target_pids.append(int(process['pid']))
        except (TypeError, ValueError):
            continue

    for pid in target_pids:
        try:
            os.kill(pid, signal.SIGTERM)
            result['killed_pids'].append(str(pid))
        except ProcessLookupError:
            continue
        except PermissionError:
            result['failed_pids'].append(str(pid))

    time.sleep(0.5)

    remaining = get_port_status(port)
    remaining_pids = []
    for process in remaining.get('processes', []):
        try:
            remaining_pids.append(int(process['pid']))
        except (TypeError, ValueError):
            continue

    for pid in remaining_pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError:
            result['failed_pids'].append(str(pid))

    time.sleep(0.2)
    final_status = get_port_status(port)
    for process in final_status.get('processes', []):
        pid = process.get('pid')
        if pid and pid not in result['failed_pids']:
            result['failed_pids'].append(pid)

    return result


def kill_all_dev_ports():
    """Kill listeners on all tracked development ports."""
    return {port: kill_port(port) for port in DEV_PORTS}


def _format_process_summary(status):
    processes = status.get('processes', [])
    if not processes:
        return "FREE"
    primary = processes[0]
    summary = f"IN USE ({primary['command']}, PID {primary['pid']})"
    if len(processes) > 1:
        summary += f" +{len(processes) - 1} more"
    return summary


def print_port_status_report(statuses=None):
    """Print a text report for tracked development ports."""
    statuses = statuses or get_port_status()
    print("\n+" + "=" * 62 + "+")
    print("|                        PORT STATUS                           |")
    print("+" + "=" * 62 + "+")
    for port, service in DEV_PORTS.items():
        status = statuses.get(port, get_port_status(port))
        state = _format_process_summary(status)
        print(f"|  {port:<5} {service:<28} {state:<25}|")
    print("+" + "=" * 62 + "+")


def _probe_http_service(port: str):
    probe = DEV_PORT_PROBES.get(str(port))
    if not probe:
        return {
            'checked': False,
            'healthy': True,
            'url': None,
            'details': 'No probe configured',
        }

    try:
        with urllib.request.urlopen(probe['url'], timeout=2) as response:
            status_code = getattr(response, 'status', None) or response.getcode()
            expected_status = probe.get('expect_status')
            healthy = expected_status is None or status_code == expected_status
            return {
                'checked': True,
                'healthy': healthy,
                'url': probe['url'],
                'status_code': status_code,
                'details': f'HTTP {status_code}',
            }
    except urllib.error.HTTPError as exc:
        expected_status = probe.get('expect_status')
        healthy = expected_status is None or exc.code == expected_status
        return {
            'checked': True,
            'healthy': healthy,
            'url': probe['url'],
            'status_code': exc.code,
            'details': f'HTTP {exc.code}',
        }
    except Exception as exc:
        return {
            'checked': True,
            'healthy': False,
            'url': probe['url'],
            'details': str(exc),
        }


def check_hung_dev_services(statuses=None):
    statuses = statuses or get_port_status()
    suspects = []

    for port, status in statuses.items():
        if not status.get('in_use'):
            continue

        probe_result = _probe_http_service(port)
        if not probe_result.get('checked') or probe_result.get('healthy'):
            continue

        suspects.append({
            'port': port,
            'service': status.get('service', DEV_PORTS.get(port, 'Unknown')),
            'processes': status.get('processes', []),
            'probe': probe_result,
        })

    return suspects


def print_hung_service_report(suspects):
    print("\n+" + "=" * 62 + "+")
    print("|                  HUNG CONNECTION CHECKER                    |")
    print("+" + "=" * 62 + "+")
    if not suspects:
        print("|  No blocked channels or hung local listeners detected.      |")
        print("+" + "=" * 62 + "+")
        return

    for suspect in suspects:
        primary = suspect['processes'][0] if suspect.get('processes') else {'command': 'unknown', 'pid': '?'}
        probe = suspect['probe']
        print(f"|  Port {suspect['port']:<4} {suspect['service']:<20} PID {primary['pid']:<8}|")
        print(f"|     Command: {primary['command']:<46}|")
        print(f"|     Probe: {probe.get('url', 'n/a'):<48}|")
        print(f"|     Result: {probe.get('details', 'unknown'):<47}|")
        print("+" + "-" * 62 + "+")


def _choose_in_use_port(statuses, prompt):
    if questionary is None:
        print("\nInstall questionary to use the interactive port manager.")
        _prompt_go_back()
        return None

    in_use_choices = [
        questionary.Choice(
            title=f"{port} - {status['service']} ({_format_process_summary(status)})",
            value=port,
        )
        for port, status in statuses.items()
        if status.get('in_use')
    ]

    if not in_use_choices:
        print("\n[OK] All tracked ports are free.")
        _prompt_go_back()
        return None

    return questionary.select(
        prompt,
        choices=in_use_choices + [questionary.Separator(), questionary.Choice(title="Back", value=None)],
        style=custom_style,
    ).ask()


def ports_menu():
    """Interactive port manager for local development diagnostics."""
    if questionary is None:
        print("\nInstall questionary to use the interactive port manager.")
        _prompt_go_back()
        return

    while True:
        clear_screen()
        statuses = get_port_status()
        print_port_status_report(statuses)

        choice = questionary.select(
            "Select port action:",
            choices=[
                "1) Refresh status",
                "2) Kill specific port",
                "3) Kill all tracked dev ports",
                "0) Back",
            ],
            style=custom_style,
        ).ask()

        if not choice or choice.startswith('0'):
            return

        if choice.startswith('1'):
            continue

        if choice.startswith('2'):
            selected_port = _choose_in_use_port(statuses, "Select port to kill:")
            if not selected_port:
                continue

            confirm = questionary.confirm(
                f"Kill listener on port {selected_port}?",
                default=False,
                style=custom_style,
            ).ask()
            if not confirm:
                continue

            result = kill_port(selected_port)
            if result.get('error'):
                print(f"\n[WARN] {result['error']}")
            elif result.get('already_free'):
                print(f"\n[OK] Port {selected_port} is already free.")
            elif result.get('failed_pids'):
                print(f"\n[WARN] Port {selected_port} still has active PIDs: {', '.join(result['failed_pids'])}")
            else:
                killed = ', '.join(result.get('killed_pids', [])) or 'unknown'
                print(f"\n[OK] Released port {selected_port} (PIDs: {killed}).")
            _prompt_go_back()
            continue

        if choice.startswith('3'):
            in_use_ports = [port for port, status in statuses.items() if status.get('in_use')]
            if not in_use_ports:
                print("\n[OK] All tracked ports are already free.")
                _prompt_go_back()
                continue

            confirm = questionary.confirm(
                f"Kill listeners on ports: {', '.join(in_use_ports)}?",
                default=False,
                style=custom_style,
            ).ask()
            if not confirm:
                continue

            results = kill_all_dev_ports()
            remaining = [port for port, result in results.items() if result.get('failed_pids')]
            released = [port for port, result in results.items() if result.get('killed_pids') and not result.get('failed_pids')]

            if released:
                print(f"\n[OK] Released ports: {', '.join(released)}")
            if remaining:
                print(f"[WARN] Could not fully release: {', '.join(remaining)}")
            if not released and not remaining:
                print("\n[OK] Nothing needed to be killed.")
            _prompt_go_back()