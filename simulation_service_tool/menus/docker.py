"""Docker Compose operations menu."""

import questionary

from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen
from simulation_service_tool.ui.display import (
    render_key_value_panel,
    render_smart_summary_panel,
    show_loading_spinner,
)
from simulation_service_tool.services.docker_compose import (
    compose_file_exists,
    is_docker_available,
    is_compose_running,
    up,
    down,
    get_service_health,
    get_logs,
    test_endpoints,
    EXPECTED_SERVICES,
)


def _prompt_go_back():
    questionary.select(
        "Next step:",
        choices=[questionary.Choice(title="Go back", value="back")],
        style=custom_style,
    ).ask()


def _docker_issue(summary, remediation):
    return {'summary': summary, 'remediation': remediation}


def _render_health_table(health):
    """Display service health as a key-value panel."""
    if '_error' in health:
        print(f"\n  [WARN] {health['_error']}\n")
        return

    rows = []
    for svc in EXPECTED_SERVICES:
        info = health.get(svc)
        if info is None:
            rows.append((svc, '[WARN] Not running'))
        elif info.get('health'):
            rows.append((svc, f"{info['state']} ({info['health']})"))
        else:
            rows.append((svc, info['state']))
    render_key_value_panel("Docker Service Health", rows)


def _render_endpoint_results(results):
    """Display endpoint test results as a key-value panel."""
    rows = []
    for r in results:
        if r['healthy']:
            rows.append((r['name'], f"[OK] {r['status']}"))
        else:
            rows.append((r['name'], f"[WARN] {r['status']}"))
    render_key_value_panel("Endpoint Tests", rows)


def ensure_docker_loaded(progress_callback=None):
    """Gather Docker Compose state snapshot."""
    progress = progress_callback or (lambda _msg: None)

    progress("Checking Docker availability...")
    docker_ok = is_docker_available()
    if not docker_ok:
        return {
            'docker_available': False,
            'compose_exists': compose_file_exists(),
            'running': False,
            'services': {},
            'issues': [_docker_issue(
                'Docker daemon is not reachable',
                'Start Docker Desktop or ensure the Docker daemon is running.',
            )],
        }

    progress("Checking docker-compose.yml...")
    has_compose = compose_file_exists()

    progress("Inspecting running containers...")
    running = is_compose_running()
    services = get_service_health() if running else {}

    issues = []
    if not has_compose:
        issues.append(_docker_issue(
            'docker-compose.yml not found',
            'Ensure you are running the CLI from the project root directory.',
        ))
    elif not running:
        issues.append(_docker_issue(
            'Docker Compose stack is not running',
            'Use "Start stack" to bring up all services with docker compose up.',
        ))
    elif '_error' not in services:
        for svc in EXPECTED_SERVICES:
            info = services.get(svc)
            if info is None:
                issues.append(_docker_issue(
                    f'{svc} container is not running',
                    f'Check logs with "docker compose logs {svc}" or rebuild with --build.',
                ))
            elif info.get('health') == 'unhealthy':
                issues.append(_docker_issue(
                    f'{svc} container is unhealthy',
                    f'Check logs: "docker compose logs {svc} --tail 50".',
                ))

    progress("Docker snapshot ready.")
    return {
        'docker_available': True,
        'compose_exists': has_compose,
        'running': running,
        'services': services,
        'issues': issues,
    }


def docker_menu():
    """Docker Compose operations menu."""
    while True:
        clear_screen()

        cache = show_loading_spinner(
            ensure_docker_loaded,
            message="Checking Docker environment...",
            timeout=15,
        )
        cache = cache or {'docker_available': False, 'services': {}, 'issues': [
            _docker_issue('Failed to load Docker state', 'Check Docker daemon and try again.'),
        ]}

        docker_ok = cache.get('docker_available', False)
        running = cache.get('running', False)
        services = cache.get('services', {})
        issues = cache.get('issues', [])

        # Status summary
        if docker_ok and running and '_error' not in services:
            healthy_count = sum(1 for s in EXPECTED_SERVICES if (services.get(s) or {}).get('running'))
            total = len(EXPECTED_SERVICES)
            rows = [
                ('Docker', '[OK] Available'),
                ('Stack', f'[OK] {healthy_count}/{total} services running'),
            ]
        elif docker_ok:
            rows = [
                ('Docker', '[OK] Available'),
                ('Stack', '[WARN] Not running'),
            ]
        else:
            rows = [
                ('Docker', '[WARN] Not available'),
                ('Stack', 'N/A'),
            ]
        render_key_value_panel("Docker Compose", rows)

        if issues:
            render_smart_summary_panel(
                "Docker Issues",
                issues=issues,
                recommendation="Address the issues below or use the menu actions.",
                border_style="yellow",
            )
        else:
            render_smart_summary_panel(
                "Docker Status",
                healthy_message="All services healthy. Stack is ready.",
                border_style="green",
            )

        # Build choices based on state
        menu_choices = []
        if not running:
            menu_choices.append("1) Start stack")
            menu_choices.append("2) Start stack (rebuild)")
        else:
            menu_choices.append("1) Health check")
            menu_choices.append("2) Test endpoints")
            menu_choices.append("3) View logs")
            menu_choices.append(questionary.Separator())
            menu_choices.append("4) Restart stack (rebuild)")
            menu_choices.append("5) Stop stack")
            menu_choices.append("6) Stop stack + remove volumes")
        menu_choices.append(questionary.Separator())
        menu_choices.append("0) Back")

        try:
            choice = questionary.select(
                "Docker operations:",
                choices=menu_choices,
                style=custom_style,
            ).ask()
        except KeyboardInterrupt:
            return

        if choice is None or choice.startswith('0'):
            return

        option = choice.split(')')[0].strip()

        if not running:
            _handle_stopped_choice(option)
        else:
            _handle_running_choice(option, services)


def _handle_stopped_choice(option):
    """Handle menu choices when stack is not running."""
    if option == '1':
        _do_up(build=False)
    elif option == '2':
        _do_up(build=True)


def _handle_running_choice(option, services):
    """Handle menu choices when stack is running."""
    if option == '1':
        _do_health()
    elif option == '2':
        _do_test_endpoints()
    elif option == '3':
        _do_logs(services)
    elif option == '4':
        _do_restart()
    elif option == '5':
        _do_down(volumes=False)
    elif option == '6':
        _do_down(volumes=True)


def _do_up(build=False):
    clear_screen()
    action = "Building and starting" if build else "Starting"
    print(f"\n  {action} Docker Compose stack...\n")
    result = up(build=build, detach=True)
    if result['success']:
        print("  [OK] Docker stack started.\n")
    else:
        print(f"  [FAIL] {result.get('error', 'Unknown error')}\n")
    _prompt_go_back()


def _do_down(volumes=False):
    clear_screen()
    action = "Stopping stack and removing volumes" if volumes else "Stopping stack"
    print(f"\n  {action}...\n")
    result = down(volumes=volumes)
    if result['success']:
        print("  [OK] Docker stack stopped.\n")
    else:
        print(f"  [FAIL] {result.get('error', 'Unknown error')}\n")
    _prompt_go_back()


def _do_restart():
    clear_screen()
    print("\n  Stopping stack...\n")
    down_result = down()
    if not down_result['success']:
        print(f"  [WARN] Stop failed: {down_result.get('error', 'unknown')}\n")
    print("  Rebuilding and starting stack...\n")
    up_result = up(build=True, detach=True)
    if up_result['success']:
        print("  [OK] Stack restarted.\n")
    else:
        print(f"  [FAIL] {up_result.get('error', 'Unknown error')}\n")
    _prompt_go_back()


def _do_health():
    clear_screen()
    health = get_service_health()
    print()
    _render_health_table(health)
    print()
    _prompt_go_back()


def _do_test_endpoints():
    clear_screen()
    print("\n  Testing endpoints...\n")
    results = test_endpoints()
    _render_endpoint_results(results)
    print()
    _prompt_go_back()


def _do_logs(services):
    service_choices = [svc for svc in EXPECTED_SERVICES if svc in services]
    service_choices.insert(0, 'all')
    service_choices.append('back')

    try:
        target = questionary.select(
            "View logs for:",
            choices=service_choices,
            style=custom_style,
        ).ask()
    except KeyboardInterrupt:
        return

    if target is None or target == 'back':
        return

    clear_screen()
    svc = None if target == 'all' else target
    result = get_logs(service=svc, tail=80)
    if result.get('error'):
        print(f"\n  [WARN] {result['error']}\n")
    else:
        print(result.get('output', ''))
    _prompt_go_back()
