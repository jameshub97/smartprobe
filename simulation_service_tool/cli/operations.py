"""High-level CLI operations for service and test management."""

import subprocess
import sys
import time

import questionary

from simulation_service_tool.cli.prompts import _prompt_go_back
from simulation_service_tool.cli.watch import watch_agents
from simulation_service_tool.services.api_client import call_service, check_service
from simulation_service_tool.services.command_runner import run_cli_command
from simulation_service_tool.services.direct_cleanup import (
    direct_completed_pods_cleanup,
    direct_full_cleanup,
    direct_preflight_check,
    direct_release_cleanup,
    direct_verify_state,
    get_test_releases,
)
from simulation_service_tool.menus.ports import get_port_status, kill_port
from simulation_service_tool.ui.display import render_status_summary
from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen


def _run_command(args, timeout=None):
    return run_cli_command(args, timeout=timeout)


def _extract_preflight_releases(preflight):
    releases = []
    for conflict in preflight.get('conflicts', []):
        if conflict.get('type') == 'helm_releases':
            releases.extend(conflict.get('releases', []))
    return [release for release in releases if release]


def show_status(service_running):
    if service_running:
        result = call_service('/api/simulation/summary')
        render_status_summary(True, result)
    else:
        state = direct_verify_state()
        render_status_summary(False, state)
    _prompt_go_back()


def hard_reset(service_running):
    clear_screen()
    print("+" + "=" * 62 + "+")
    print("|                        HARD RESET                           |")
    print("+" + "=" * 62 + "+")
    print("\n  This will:")
    print("   - Uninstall all detected test Helm releases")
    print("   - Delete Playwright StatefulSet resources")
    print("   - Remove Playwright pods, jobs, PVCs, and PDBs")
    print("   - Delete completed test pods")
    print("   - Reset the cluster to a clean test state")

    confirm = questionary.confirm(
        'This will delete all current test resources. Continue?',
        default=False,
        style=custom_style,
    ).ask()
    if not confirm:
        print("\n[CANCELLED] Hard reset aborted.")
        _prompt_go_back()
        return

    print("\n[INFO] Running hard reset...")
    releases = _extract_preflight_releases(direct_preflight_check())
    actions = []

    for release in releases:
        result = direct_release_cleanup(release, dry_run=False)
        actions.append((f"helm release {release}", result.get('error') or result.get('warning') or result.get('helm') or 'processed'))

    direct_full_cleanup(dry_run=False)
    actions.append(("full cleanup", 'completed'))

    completed_cleanup = direct_completed_pods_cleanup(dry_run=False)
    actions.append(("completed pods cleanup", completed_cleanup.get('error') or f"removed {completed_cleanup.get('count', 0)} completed pods"))

    reset_commands = [
        ('kubectl delete statefulset playwright-agent', ['kubectl', 'delete', 'statefulset', 'playwright-agent', '--ignore-not-found']),
        ('kubectl delete jobs -l app=playwright-agent', ['kubectl', 'delete', 'jobs', '-l', 'app=playwright-agent', '--ignore-not-found']),
        (
            'kubectl delete pods -l app=playwright-agent --force --grace-period=0',
            ['kubectl', 'delete', 'pods', '-l', 'app=playwright-agent', '--force', '--grace-period=0', '--ignore-not-found'],
        ),
        ('kubectl delete pvc playwright-cache', ['kubectl', 'delete', 'pvc', 'playwright-cache', '--ignore-not-found']),
        ('kubectl delete pdb playwright-agent-pdb', ['kubectl', 'delete', 'pdb', 'playwright-agent-pdb', '--ignore-not-found']),
    ]
    for title, args in reset_commands:
        result = _run_command(args)
        actions.append((title, result.stdout.strip() or result.stderr.strip() or 'processed'))

    state = direct_verify_state()
    print("\n[OK] Hard reset complete.")
    for title, outcome in actions[:12]:
        print(f"   - {title}: {outcome}")
    if len(actions) > 12:
        print(f"   ... and {len(actions) - 12} more actions")

    if state.get('is_clean'):
        print("\n[OK] Cluster is clean and ready.")
    else:
        print(
            f"\n[WARN] Remaining resources detected: releases={state.get('helm_test_releases', '?')}, "
            f"pods={state.get('playwright_pods', '?')}, pvcs={state.get('playwright_pvcs', '?')}, pdbs={state.get('conflicting_pdbs', '?')}"
        )

    _prompt_go_back('Return to dashboard')


def _get_active_tests(service_running):
    if service_running:
        tests = call_service('/api/simulation/tests')
        if isinstance(tests, list):
            if tests:
                return tests

            direct_releases = get_test_releases()
            if direct_releases:
                return [
                    {
                        'name': release,
                        'status': 'unknown',
                        'updated': 'direct fallback',
                    }
                    for release in direct_releases
                ]
            return []

        direct_releases = get_test_releases()
        return [
            {
                'name': release,
                'status': 'unknown',
                'updated': 'direct fallback',
            }
            for release in direct_releases
        ]

    releases = get_test_releases()
    return [
        {
            'name': release,
            'status': 'unknown',
            'updated': 'direct mode',
        }
        for release in releases
    ]


def _format_test_choice(test):
    name = test.get('name', 'unknown')
    status = test.get('status', 'unknown')
    updated = test.get('updated', 'n/a')
    return f"{name} | {status} | {updated}"


def _stop_release(service_running, release_name):
    if service_running:
        return call_service('/api/simulation/stop', 'POST', {'name': release_name})
    return direct_release_cleanup(release_name, dry_run=False)


def stop_test_menu(service_running):
    clear_screen()
    print("+" + "=" * 62 + "+")
    print("|                         STOP A TEST                         |")
    print("+" + "=" * 62 + "+")

    tests = _get_active_tests(service_running)
    if not tests:
        print("\n  [OK] No active tests found.")
        _prompt_go_back()
        return

    print(f"\n  Active tests: {len(tests)}\n")
    for test in tests[:10]:
        print(f"   - {_format_test_choice(test)}")
    if len(tests) > 10:
        print(f"   ... and {len(tests) - 10} more")

    action = questionary.select(
        "How would you like to stop tests?",
        choices=[
            questionary.Choice(title="Stop one test", value="one"),
            questionary.Choice(title="Stop multiple tests", value="multiple"),
            questionary.Choice(title="Stop all active tests", value="all"),
            questionary.Choice(title="Back", value="back"),
        ],
        style=custom_style,
    ).ask()

    if not action or action == 'back':
        return

    selected_names = []
    if action == 'one':
        release_name = questionary.select(
            "Select test to stop:",
            choices=[
                *[questionary.Choice(title=_format_test_choice(test), value=test['name']) for test in tests],
                questionary.Separator(),
                questionary.Choice(title="Back", value=None),
            ],
            style=custom_style,
        ).ask()
        if not release_name:
            return
        selected_names = [release_name]
    elif action == 'multiple':
        selected_names = questionary.checkbox(
            "Select tests to stop:",
            choices=[questionary.Choice(title=_format_test_choice(test), value=test['name']) for test in tests],
            style=custom_style,
        ).ask() or []
        if not selected_names:
            return
    elif action == 'all':
        selected_names = [test['name'] for test in tests]

    confirm = questionary.confirm(
        f"Stop {len(selected_names)} test{'s' if len(selected_names) != 1 else ''}?",
        default=False,
        style=custom_style,
    ).ask()
    if not confirm:
        return

    print()
    successes = []
    failures = []
    for release_name in selected_names:
        print(f"Stopping: {release_name}")
        result = _stop_release(service_running, release_name)
        if result.get('success') or result.get('helm') == 'uninstalled':
            successes.append(release_name)
            continue
        warning = result.get('warning')
        error = result.get('error') or warning or 'Unknown stop error'
        failures.append((release_name, error))

    if successes:
        print(f"\n[OK] Stopped {len(successes)} test{'s' if len(successes) != 1 else ''}: {', '.join(successes)}")
    if failures:
        print("[WARN] Some tests could not be stopped:")
        for release_name, error in failures:
            print(f"   - {release_name}: {error}")

    _prompt_go_back()


def list_tests(service_running):
    print("\n[LIST] List Tests - Coming soon!")
    _prompt_go_back()


def watch_progress(service_running):
    clear_screen()
    if service_running:
        tests = call_service('/api/simulation/tests')
        if isinstance(tests, list) and tests:
            choices = [questionary.Choice(title=test['name'], value=test['name']) for test in tests]
            choices.append(questionary.Separator())
            choices.append(questionary.Choice(title="Watch all", value=None))
            choices.append(questionary.Choice(title="Back", value="__back__"))
            release = questionary.select(
                "Which test to watch?",
                choices=choices,
                style=custom_style,
            ).ask()
            if release == "__back__":
                return
            watch_agents(release, service_running)
        else:
            print("\n  No running tests found.")
            _prompt_go_back()
    else:
        watch_agents(None, service_running)


def start_service():
    port_status = get_port_status('5002')
    if port_status.get('error'):
        print(f"\n[WARN] Could not inspect port 5002: {port_status['error']}")

    if port_status.get('in_use'):
        primary = port_status['processes'][0]
        print("\n[WARN] Port 5002 is already in use.")
        print(f"   Listener: {primary['command']} (PID {primary['pid']})")

        choices = []
        if check_service():
            choices.append(questionary.Choice(title="Use existing service", value="reuse"))
        choices.extend([
            questionary.Choice(title="Kill existing process and start fresh", value="restart"),
            questionary.Choice(title="Start anyway (may fail)", value="force"),
            questionary.Choice(title="Cancel", value="cancel"),
        ])

        action = questionary.select(
            "What would you like to do?",
            choices=choices,
            style=custom_style,
        ).ask()

        if action == "cancel" or not action:
            return
        if action == "reuse":
            print("\n[OK] Using the existing simulation service.")
            _prompt_go_back()
            return
        if action == "restart":
            kill_result = kill_port('5002')
            if kill_result.get('failed_pids'):
                print(f"\n[WARN] Could not release port 5002 from PIDs: {', '.join(kill_result['failed_pids'])}")
                _prompt_go_back()
                return
            time.sleep(0.5)

    print("\nStarting simulation service...")
    subprocess.Popen([sys.executable, "simulation_service.py", "server", "--port", "5002"])
    print("   Service starting on http://localhost:5002")
    time.sleep(2)
    if check_service():
        print("   [OK] Service is reachable.")
    else:
        print("   [WARN] Service did not respond yet. Check Diagnostics -> Service Health.")
    _prompt_go_back()