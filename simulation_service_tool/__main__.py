"""Entry point for `python -m simulation_service_tool`."""

import sys
from simulation_service_tool.menus.ports import DEV_PORTS, get_port_status, kill_port


def _handle_docker_command():
    from simulation_service_tool.services.docker_compose import (
        is_docker_available,
        is_compose_running,
        up,
        down,
        get_service_health,
        get_logs,
        test_endpoints,
        EXPECTED_SERVICES,
    )

    sub = sys.argv[2] if len(sys.argv) > 2 else 'status'

    if sub == 'up':
        build = '--build' in sys.argv
        if not is_docker_available():
            print("[FAIL] Docker daemon is not reachable.")
            sys.exit(1)
        result = up(build=build, detach=True)
        if result['success']:
            print("[OK] Docker stack started.")
        else:
            print(f"[FAIL] {result.get('error', 'Unknown error')}")
            sys.exit(1)
        sys.exit(0)

    elif sub == 'down':
        volumes = '-v' in sys.argv or '--volumes' in sys.argv
        result = down(volumes=volumes)
        if result['success']:
            print("[OK] Docker stack stopped.")
        else:
            print(f"[FAIL] {result.get('error', 'Unknown error')}")
            sys.exit(1)
        sys.exit(0)

    elif sub == 'health':
        if not is_compose_running():
            print("[WARN] Docker Compose stack is not running.")
            sys.exit(1)
        health = get_service_health()
        if '_error' in health:
            print(f"[FAIL] {health['_error']}")
            sys.exit(1)
        for svc in EXPECTED_SERVICES:
            info = health.get(svc)
            if info is None:
                print(f"  {svc}: NOT RUNNING")
            elif info.get('health'):
                print(f"  {svc}: {info['state']} ({info['health']})")
            else:
                print(f"  {svc}: {info['state']}")
        sys.exit(0)

    elif sub == 'test':
        results = test_endpoints()
        all_ok = True
        for r in results:
            status = "[OK]" if r['healthy'] else "[FAIL]"
            print(f"  {status} {r['name']}: {r['status']}")
            if not r['healthy']:
                all_ok = False
        sys.exit(0 if all_ok else 1)

    elif sub == 'logs':
        svc = sys.argv[3] if len(sys.argv) > 3 else None
        result = get_logs(service=svc, tail=80)
        if result.get('error'):
            print(f"[FAIL] {result['error']}")
            sys.exit(1)
        print(result.get('output', ''))
        sys.exit(0)

    elif sub in ('status', 'ps'):
        docker_ok = is_docker_available()
        running = is_compose_running() if docker_ok else False
        print(f"Docker daemon: {'available' if docker_ok else 'NOT available'}")
        print(f"Compose stack: {'running' if running else 'not running'}")
        if running:
            health = get_service_health()
            if '_error' not in health:
                for svc in EXPECTED_SERVICES:
                    info = health.get(svc)
                    if info is None:
                        print(f"  {svc}: NOT RUNNING")
                    else:
                        label = f"{info['state']}"
                        if info.get('health'):
                            label += f" ({info['health']})"
                        print(f"  {svc}: {label}")
        sys.exit(0)

    else:
        print("Usage: python3 simulation_service_tool.py docker <command>")
        print()
        print("Commands:")
        print("  up [--build]       Start the Docker Compose stack")
        print("  down [-v]          Stop the stack (optionally remove volumes)")
        print("  health             Show health of all services")
        print("  test               Test health endpoints")
        print("  logs [service]     View logs (optionally for one service)")
        print("  status             Show Docker and stack status")
        sys.exit(0)


def _handle_ports_command():
    if '--kill' in sys.argv:
        kill_index = sys.argv.index('--kill')
        if kill_index + 1 >= len(sys.argv):
            print("\nUsage: python3 simulation_service_tool.py ports --kill <port>")
            sys.exit(1)

        port = sys.argv[kill_index + 1]
        result = kill_port(port)
        if result.get('error'):
            print(f"[WARN] {result['error']}")
            sys.exit(1)
        if result.get('already_free'):
            print(f"[OK] Port {port} is already free.")
            sys.exit(0)
        if result.get('failed_pids'):
            print(f"[WARN] Port {port} still has active PIDs: {', '.join(result['failed_pids'])}")
            sys.exit(1)

        killed = ', '.join(result.get('killed_pids', [])) or 'unknown'
        print(f"[OK] Released port {port} (PIDs: {killed}).")
        sys.exit(0)

    statuses = get_port_status()
    for port, service in DEV_PORTS.items():
        status = statuses[port]
        if status.get('error'):
            state = f"ERROR ({status['error']})"
        elif status.get('in_use'):
            primary = status['processes'][0]
            state = f"IN USE ({primary['command']}, PID {primary['pid']})"
        else:
            state = "FREE"
        print(f"{port}: {service} - {state}")
    sys.exit(0)


def run():
    # Command-line direct cleanup support
    if len(sys.argv) > 1 and sys.argv[1] in {'cleanup', 'clean'}:
        from simulation_service_tool.ui.display import display_cleanup_result, display_verification_result
        from simulation_service_tool.services.direct_cleanup import (
            direct_quick_cleanup,
            direct_full_cleanup,
            direct_stuck_cleanup,
            direct_verify_state,
            get_quick_cleanup_commands,
        )

        if '--quick' in sys.argv:
            print("\nRunning quick clean (direct)...")
            result = direct_quick_cleanup(dry_run=False)
            display_cleanup_result(result, dry_run=False)
        elif '--show-commands' in sys.argv:
            print("\nQuick clean commands:")
            for command in get_quick_cleanup_commands():
                print(f"   {' '.join(command)}")
        elif '--full' in sys.argv:
            print("\nRunning full reset (direct)...")
            result = direct_full_cleanup(dry_run=False)
            display_cleanup_result(result, dry_run=False)
        elif '--stuck' in sys.argv:
            print("\nCleaning stuck resources (direct)...")
            result = direct_stuck_cleanup(dry_run=False)
            display_cleanup_result(result, dry_run=False)
        elif '--verify' in sys.argv:
            print("\nVerifying cluster state (direct)...")
            result = direct_verify_state()
            display_verification_result(result)
        else:
            print("\nUsage: python3 simulation_service_tool.py clean --quick|--show-commands|--full|--stuck|--verify")
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == 'ports':
        _handle_ports_command()

    if len(sys.argv) > 1 and sys.argv[1] == 'docker':
        _handle_docker_command()

    if len(sys.argv) == 1:
        try:
            from simulation_service_tool.cli.main import interactive_menu
            interactive_menu()
        except ImportError:
            print("Agent Control CLI")
            print("=" * 40)
            print("Commands: start, stop, list, status, watch, presets, server, clean, cleanup, ports")
            print("\nExample: python3 simulation_service_tool.py status")
            print("\nInstall questionary for interactive menu: pip3 install questionary")
    else:
        from simulation_service import main as service_main
        service_main()


if __name__ == "__main__":
    run()
