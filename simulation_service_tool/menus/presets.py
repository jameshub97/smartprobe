"""Preset menu and configuration."""

import time
import questionary

from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen
from simulation_service_tool.services.api_client import check_service, call_service


def _prompt_go_back():
    questionary.select(
        "Next step:",
        choices=[questionary.Choice(title="Go back", value="back")],
        style=custom_style,
    ).ask()


def get_preset_config(preset):
    """Get configuration for a preset."""
    presets = {
        'tiny': {'completions': 5, 'parallelism': 2, 'persona': 'impatient', 'mode': 'basic'},
        'small': {'completions': 10, 'parallelism': 5, 'persona': 'impatient', 'mode': 'basic'},
        'medium': {'completions': 50, 'parallelism': 10, 'persona': 'strategic', 'mode': 'basic'},
        'large': {'completions': 100, 'parallelism': 20, 'persona': 'browser', 'mode': 'basic'},
        'xlarge': {'completions': 500, 'parallelism': 50, 'persona': 'browser', 'mode': 'basic'},
        'throughput': {
            'completions': 500,
            'parallelism': 100,
            'persona': 'impatient',
            'mode': 'basic',
            'workers': 1,
            'replicaCount': 100,
            'shardTotal': 100,
            'requestMemory': '64Mi',
            'requestCpu': '50m',
            'limitMemory': '128Mi',
            'limitCpu': '200m',
            'backoffLimit': 0,
            'ttlSecondsAfterFinished': 0,
        },
    }
    return presets.get(preset, presets['small'])


def show_presets():
    """Show available presets with option to select and start a test."""
    clear_screen()
    print("+" + "=" * 62 + "+")
    print("|                      AVAILABLE PRESETS                       |")
    print("+" + "=" * 62 + "+")
    print("|                                                              |")
    print("|  tiny   | 5 agents   | 2 parallel  | ~10s  | Quick check    |")
    print("|  small  | 10 agents  | 5 parallel  | ~30s  | Dev testing    |")
    print("|  medium | 50 agents  | 10 parallel | ~2m   | Integration    |")
    print("|  large  | 100 agents | 20 parallel | ~5m   | Performance    |")
    print("|  xlarge | 500 agents | 50 parallel | ~15m  | Stress test    |")
    print("|                                                              |")
    print("+" + "=" * 62 + "+")
    action = questionary.select(
        "What would you like to do?",
        choices=[
            questionary.Choice(title="Start a test with one of these presets", value="start"),
            questionary.Choice(title="Back to main menu", value="back"),
        ],
        style=custom_style
    ).ask()
    if action == "back":
        return
    preset = questionary.select(
        "Choose preset:",
        choices=[
            questionary.Choice(title="tiny   | 5 agents   | 2 parallel  | ~10s", value="tiny"),
            questionary.Choice(title="small  | 10 agents  | 5 parallel  | ~30s", value="small"),
            questionary.Choice(title="medium | 50 agents  | 10 parallel | ~2m", value="medium"),
            questionary.Choice(title="large  | 100 agents | 20 parallel | ~5m", value="large"),
            questionary.Choice(title="xlarge | 500 agents | 50 parallel | ~15m", value="xlarge"),
            questionary.Separator(),
            questionary.Choice(title="Back", value="back"),
        ],
        style=custom_style
    ).ask()
    if preset == "back":
        return
    default_name = f"{preset}-{int(time.time())}"
    name = questionary.text(
        "Test name (leave blank for auto-generated):",
        default=""
    ).ask()
    if not name:
        name = default_name
    print(f"\nStarting test: {name}")
    print(f"   Preset: {preset}")
    service_running = check_service()
    if service_running:
        result = call_service('/api/simulation/start', 'POST', {
            'preset': preset,
            'name': name
        })
        if 'error' in result:
            print(f"[ERROR] {result['error']}")
        else:
            print("[OK] Test started successfully!")
    else:
        print("[WARN] Service not running - direct helm not yet implemented")
        print(f"   Would run: helm install {name} ./helm/playwright-agent --set completions=...")
    _prompt_go_back()
