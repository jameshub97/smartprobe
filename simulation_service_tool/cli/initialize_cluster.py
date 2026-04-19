"""Interactive cluster initialization menu."""

import questionary

from simulation_service_tool.cli.prompts import _prompt_go_back
from simulation_service_tool.services.cluster_init import (
    clear_initialized,
    initialize_cluster,
    is_initialized,
)
from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen


def initialize_cluster_menu():
    """Interactive one-time cluster initialization."""
    clear_screen()

    if is_initialized():
        print("\n  ✅ Cluster already initialized this session.")
        reinit = questionary.confirm(
            "Re-initialize anyway?",
            default=False,
            style=custom_style,
        ).ask()
        if not reinit:
            _prompt_go_back()
            return

    clear_initialized()

    try:
        from rich.console import Console
        _console = Console()
        _has_rich = True
    except ImportError:
        _has_rich = False

    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║                   INITIALIZING CLUSTER                      ║")
    print("╠══════════════════════════════════════════════════════════════╣")

    def _progress(msg):
        if _has_rich:
            _console.print(f"  [dim]{msg}[/dim]")
        else:
            print(f"  {msg}")

    success, results = initialize_cluster(progress_callback=_progress)

    print("║                                                              ║")
    for name, result in results:
        icon = "✅" if result['success'] else "❌"
        detail = result.get('detail', '')
        short_detail = f" ({detail})" if detail and len(detail) < 40 else ""
        print(f"║  {icon} {name:<30}{short_detail:<26} ║")
    print("║                                                              ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    if success:
        print("\n  ✅ Cluster initialized. Ready to run tests.\n")
        _prompt_go_back()
        return

    print("\n  ❌ Initialization incomplete. Check output above.\n")

    failed_steps = [name for name, result in results if not result['success']]
    kubeconfig_failed = any('kubeconfig' in step.lower() or 'node' in step.lower() for step in failed_steps)

    choices = [questionary.Choice(title="Retry initialization", value="retry")]
    if kubeconfig_failed:
        choices.append(questionary.Choice(
            title="Open a shell to fix kubeconfig (run: kubectl config use-context <ctx>)",
            value="hint_kubeconfig",
        ))
    choices.append(questionary.Choice(
        title="Skip and continue anyway (cluster may not be clean)",
        value="skip",
    ))
    choices.append(questionary.Separator())
    choices.append(questionary.Choice(title="Go back", value="back"))

    action = questionary.select(
        "What would you like to do?",
        choices=choices,
        style=custom_style,
    ).ask()

    if action == "retry":
        initialize_cluster_menu()
    elif action == "hint_kubeconfig":
        print()
        print("  To fix kubeconfig, run one of:")
        print("    kubectl config get-contexts          # list available contexts")
        print("    kubectl config use-context <name>   # switch context")
        print("    docker desktop → Kubernetes → Enable Kubernetes (if using Docker Desktop)")
        print()
        _prompt_go_back()
    elif action == "skip":
        from simulation_service_tool.services.cluster_init import set_initialized

        set_initialized()
        print("  [36m[INFO][0m Skipped initialization — flag set so this won't prompt again this session.")
        _prompt_go_back()