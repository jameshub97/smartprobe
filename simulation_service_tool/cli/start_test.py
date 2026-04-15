"""Test launch flow."""

import time

import questionary

from simulation_service_tool.cli.initialize_cluster import initialize_cluster_menu
from simulation_service_tool.cli.preflight import (
    _auto_fix_conflicts,
    _get_preflight,
    _handle_remaining_preflight_conflicts,
    _handle_start_error_recovery,
)
from simulation_service_tool.cli.prompts import _prompt_advanced_test_options, _prompt_go_back
from simulation_service_tool.cli.watch import watch_agents, watch_release_pods_kubectl
from simulation_service_tool.menus.presets import get_preset_config
from simulation_service_tool.services.api_client import call_service
from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen


def start_test_menu(service_running):
    """Start a test with preset or custom configuration."""
    clear_screen()
    print("+" + "=" * 62 + "+")
    print("|                        START A TEST                          |")
    print("+" + "=" * 62 + "+")
    method = questionary.select(
        "How would you like to configure the test?",
        choices=[
            questionary.Choice(title="Use a preset (recommended)", value="preset"),
            questionary.Choice(title="Custom configuration", value="custom"),
            questionary.Separator(),
            questionary.Choice(title="Back", value="back"),
        ],
        style=custom_style
    ).ask()
    if method == "back":
        return
    # Choose simulation mode
    mode = questionary.select(
        "Simulation mode:",
        choices=[
            questionary.Choice(title="basic - Standard Playwright e2e test", value="basic"),
            questionary.Choice(title="transactional - Browse & transfer assets (agent)", value="transactional"),
            questionary.Separator(),
            questionary.Choice(title="Back", value="back"),
        ],
        style=custom_style
    ).ask()
    if mode == "back":
        return

    if method == "preset":
        preset = questionary.select(
            "Choose preset:",
            choices=[
                questionary.Choice(title="tiny   | 5 agents   | 2 parallel  | ~10s", value="tiny"),
                questionary.Choice(title="small  | 10 agents  | 5 parallel  | ~30s", value="small"),
                questionary.Choice(title="medium | 50 agents  | 10 parallel | ~2m", value="medium"),
                questionary.Choice(title="large  | 100 agents | 20 parallel | ~5m", value="large"),
                questionary.Choice(title="xlarge | 500 agents | 50 parallel | ~15m", value="xlarge"),
                questionary.Choice(title="throughput | 500 agents | 100 parallel | low resource", value="throughput"),
                questionary.Separator(),
                questionary.Choice(title="Back", value="back"),
            ],
            style=custom_style
        ).ask()
        if preset == "back":
            return
        config = get_preset_config(preset)
        config['mode'] = mode
    else:
        completions = questionary.text(
            "Number of agents (completions):",
            default="50",
            validate=lambda text: text.isdigit() and int(text) > 0 or "Must be a positive number"
        ).ask()
        parallelism = questionary.text(
            "Max concurrent agents (parallelism):",
            default=str(min(10, int(completions))),
            validate=lambda text: text.isdigit() and int(text) > 0 or "Must be a positive number"
        ).ask()
        persona = questionary.select(
            "Agent persona:",
            choices=[
                questionary.Choice(title="strategic - Thoughtful, variable timing", value="strategic"),
                questionary.Choice(title="browser - Realistic human behavior", value="browser"),
                questionary.Choice(title="impatient - Fast clicks, minimal waits", value="impatient"),
            ],
            style=custom_style
        ).ask()
        config = {
            'completions': int(completions),
            'parallelism': int(parallelism),
            'persona': persona,
            'mode': mode
        }
        if not _prompt_advanced_test_options(config):
            _prompt_go_back()
            return
        preset = "custom"
    default_name = f"{preset}-{int(time.time())}"
    name = questionary.text(
        "Test name (leave blank for auto-generated):",
        default=""
    ).ask()
    if not name:
        name = default_name
    print(f"\n[CONFIG] Test Configuration:")
    print(f"   Name: {name}")
    print(f"   Mode: {config.get('mode', 'basic')}")
    print(f"   Agents: {config.get('completions', '?')}")
    print(f"   Parallel: {config.get('parallelism', '?')}")
    print(f"   Persona: {config.get('persona', 'browser')}")
    if config.get('workers'):
        print(f"   Workers per pod: {config['workers']}")
    if config.get('replicaCount'):
        print(f"   Replica count: {config['replicaCount']}")
    if config.get('shardTotal'):
        print(f"   Shard total: {config['shardTotal']}")
    if config.get('requestMemory') and config.get('requestCpu'):
        print(f"   Requests: {config['requestCpu']} CPU / {config['requestMemory']} memory")
    if config.get('limitMemory') and config.get('limitCpu'):
        print(f"   Limits: {config['limitCpu']} CPU / {config['limitMemory']} memory")
    if 'backoffLimit' in config:
        print(f"   Backoff limit: {config['backoffLimit']}")
    if 'ttlSecondsAfterFinished' in config:
        print(f"   TTL after finished: {config['ttlSecondsAfterFinished']}s")
    if config.get('wait'):
        print("   Wait for ready: yes")
    if config.get('imageRepository') and config.get('imageTag'):
        print(f"   Image: {config['imageRepository']}:{config['imageTag']}")
    if config.get('commandOverride'):
        print(f"   Command override: {config['commandOverride']}")
    if config.get('kueue'):
        print("   Kueue queuing: enabled")
    confirm = questionary.confirm("Start this test?", default=True).ask()
    if not confirm:
        print("[CANCELLED]")
        _prompt_go_back()
        return
    print(f"\nStarting test: {name}")
    if service_running:
        print("[INFO] Probing service health...")
        from simulation_service_tool.services.api_client import check_service
        if not check_service():
            print("[WARN] Simulation service is not responding. Preflight will fall back to direct mode.")
            service_running = False

        if service_running:
            print("[INFO] Running preflight conflict check via service API...")
        else:
            print("[INFO] Running preflight conflict check via kubectl...")
        preflight = _get_preflight(service_running)
        if preflight.get('cancelled'):
            print("[CANCELLED] Preflight check was cancelled.")
            _prompt_go_back()
            return
        if preflight.get('error'):
            print(f"[WARN] Could not run preflight checks: {preflight['error']}")
            action = questionary.select(
                "How would you like to proceed?",
                choices=[
                    questionary.Choice(title="Start the simulation service and retry", value="start_service"),
                    questionary.Choice(title="Continue anyway (skip preflight)", value="continue"),
                    questionary.Choice(title="Go back", value="back"),
                ],
                style=custom_style,
            ).ask()
            if action == "start_service":
                from simulation_service_tool.services.smart_diagnostics import _restart_service
                print("[INFO] Starting simulation service...")
                success, detail = _restart_service()
                print(f"{'[OK]' if success else '[WARN]'} {detail}")
                if success:
                    print("[INFO] Retrying preflight check...")
                    service_running = True
                    preflight = _get_preflight(service_running)
                    if preflight.get('cancelled'):
                        print("[CANCELLED] Preflight check was cancelled.")
                        _prompt_go_back()
                        return
                    if preflight.get('error'):
                        print(f"[WARN] Preflight still unavailable: {preflight['error']}")
                        preflight = {'has_conflicts': False}
                else:
                    _prompt_go_back()
                    return
            elif action == "continue":
                preflight = {'has_conflicts': False}
            else:
                return

        if preflight.get('has_conflicts'):
            print("[INFO] Clearing conflicting resources...")
            attempted_cleanup = _auto_fix_conflicts(preflight)
            if attempted_cleanup:
                print("[OK] Standard conflicts cleared.")

            print("[INFO] Re-checking cluster state...")
            preflight = _get_preflight(service_running)
            if preflight.get('cancelled'):
                print("[CANCELLED] Preflight refresh was cancelled.")
                _prompt_go_back()
                return
            if preflight.get('error'):
                print(f"[WARN] Could not refresh preflight checks: {preflight['error']}")
                _prompt_go_back()
                return
            if preflight.get('has_conflicts'):
                if not _handle_remaining_preflight_conflicts(preflight, service_running):
                    print("[CANCELLED]")
                    _prompt_go_back()
                    return

        payload = {
            'name': name,
            'skip_preflight': True,  # Already checked and cleaned above
            **config
        }

        # Auto-detect Kueue if not explicitly set by advanced options
        if 'kueue' not in payload:
            try:
                from simulation_service_tool.services.kueue import is_kueue_installed
                if is_kueue_installed():
                    payload['kueue'] = True
                    print("[INFO] Kueue detected on cluster — enabling workload queuing.")
            except Exception:
                pass

        result = call_service('/api/simulation/start', 'POST', payload)
        if 'error' in result:
            print(f"[ERROR] {result['error']}")
            _handle_start_error_recovery(result['error'], service_running)
            return
        else:
            print("[OK] Test started successfully!")
    else:
        # Auto-detect Kueue for direct helm path
        if 'kueue' not in config:
            try:
                from simulation_service_tool.services.kueue import is_kueue_installed
                if is_kueue_installed():
                    config['kueue.enabled'] = True
                    config['kueue.queueName'] = 'simulation-queue'
                    print("[INFO] Kueue detected — enabling workload queuing.")
            except Exception:
                pass
        cmd = f"helm install {name} ./helm/playwright-agent"
        for key, value in config.items():
            cmd += f" --set {key}={value}"
        print(f"   Running: {cmd}")
        # subprocess.run(cmd, shell=True)  # Uncomment to actually run

    # Offer to watch agents live
    watch_choice = questionary.select(
        "What would you like to do next?",
        choices=[
            questionary.Choice(title="Watch agents live (recommended)", value="watch"),
            questionary.Choice(title="Watch pods with kubectl -w", value="kubectl_watch"),
            questionary.Choice(title="Return to main menu", value="menu"),
        ],
        style=custom_style
    ).ask()
    if watch_choice == "watch":
        watch_agents(name, service_running)
    elif watch_choice == "kubectl_watch":
        watch_release_pods_kubectl(name)
    else:
        _prompt_go_back("Return to main menu")
