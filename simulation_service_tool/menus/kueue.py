"""Kueue workload queuing management menu."""

import questionary

from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen
from simulation_service_tool.ui.display import render_key_value_panel, show_loading_spinner
from simulation_service_tool.services.kueue import (
    is_kueue_installed,
    install_kueue,
    uninstall_kueue,
    apply_queues,
    delete_queues,
    get_cluster_queue_status,
    get_local_queue_status,
    list_workloads,
)
from simulation_service_tool.cli.prompts import _prompt_go_back


def _load_kueue_state(progress_callback=None):
    progress = progress_callback or (lambda _: None)
    progress("Checking Kueue CRDs...")
    installed = is_kueue_installed()
    cq = lq = None
    if installed:
        progress("Loading ClusterQueue status...")
        cq = get_cluster_queue_status()
        progress("Loading LocalQueue status...")
        lq = get_local_queue_status()
    return {'installed': installed, 'cq': cq or {}, 'lq': lq or {}}


def kueue_menu():
    """Interactive Kueue sub-menu."""
    while True:
        clear_screen()

        state = show_loading_spinner(_load_kueue_state, message="Loading Kueue status...")
        if state is None:
            return
        installed = state['installed']
        cq = state['cq']
        lq = state['lq']

        status_label = "[OK] Installed" if installed else "[--] Not installed"
        print(f"\n  Kueue CRDs: {status_label}")

        if installed:
            if cq.get("exists"):
                rows = [
                    ("Pending workloads", str(cq["pending_workloads"])),
                    ("Admitted workloads", str(cq["admitted_workloads"])),
                ]
                for res, quota in cq.get("quotas", {}).items():
                    rows.append((f"Quota: {res}", str(quota)))
                render_key_value_panel("ClusterQueue", rows)
            if lq.get("exists"):
                rows = [
                    ("Pending", str(lq["pending_workloads"])),
                    ("Admitted", str(lq["admitted_workloads"])),
                ]
                render_key_value_panel("LocalQueue", rows)

        choices = _build_choices(installed)

        try:
            choice = questionary.select(
                "Kueue action:",
                choices=choices,
                style=custom_style,
            ).ask()
        except KeyboardInterrupt:
            return

        if choice is None or choice.startswith("0"):
            return

        option = choice.split(")")[0].strip()
        _handle_choice(option, installed)


def _build_choices(installed: bool) -> list:
    if installed:
        return [
            "1) Queue Status & Workloads",
            "2) Apply Queues (ResourceFlavor / ClusterQueue / LocalQueue)",
            "3) Delete Queues",
            "4) Uninstall Kueue",
            questionary.Separator(),
            "0) Back",
        ]
    return [
        "1) Install Kueue",
        "2) Apply Queues",
        questionary.Separator(),
        "0) Back",
    ]


def _handle_choice(option: str, installed: bool):
    if installed:
        actions = {
            "1": _show_workloads,
            "2": _do_apply_queues,
            "3": _do_delete_queues,
            "4": _do_uninstall,
        }
    else:
        actions = {
            "1": _do_install,
            "2": _do_apply_queues,
        }
    action = actions.get(option)
    if action:
        action()


def _show_workloads():
    clear_screen()
    print("+" + "=" * 62 + "+")
    print("|              KUEUE WORKLOADS                                 |")
    print("+" + "=" * 62 + "+")

    workloads = show_loading_spinner(list_workloads, message="Fetching workloads...")
    if not workloads:
        print("\n  No workloads found.")
    else:
        rows = [
            (w["name"], "admitted" if w["admitted"] else "pending")
            for w in workloads
        ]
        render_key_value_panel("Workloads", rows)

    _prompt_go_back()


def _do_apply_queues():
    clear_screen()
    print("\n  Applying Kueue queue manifests...")
    result = apply_queues()
    if result["success"]:
        print("  [OK] Queues applied.")
        if result.get("stdout"):
            for line in result["stdout"].strip().split("\n")[:10]:
                print(f"    {line}")
    else:
        print("  [FAIL] Could not apply queues.")
        if result.get("stderr"):
            for line in result["stderr"].strip().split("\n")[:10]:
                print(f"    {line}")
    _prompt_go_back()


def _do_delete_queues():
    clear_screen()
    confirm = questionary.confirm(
        "Delete all Kueue queue resources?",
        default=False,
        style=custom_style,
    ).ask()
    if not confirm:
        return
    print("\n  Deleting Kueue queues...")
    result = delete_queues()
    if result["success"]:
        print("  [OK] Queues removed.")
    else:
        print("  [FAIL] Delete failed.")
        if result.get("stderr"):
            for line in result["stderr"].strip().split("\n")[:10]:
                print(f"    {line}")
    _prompt_go_back()


def _do_install():
    clear_screen()
    print("\n  Installing Kueue (this may take a minute)...\n")
    result = install_kueue()
    if result["success"]:
        print("  [OK] Kueue installed.")
        print("\n  Applying queue manifests...")
        qr = apply_queues()
        if qr["success"]:
            print("  [OK] Queues applied.")
        else:
            print("  [WARN] Queue apply failed — you can retry from the menu.")
    else:
        print("  [FAIL] Installation failed.")
        if result.get("stderr"):
            for line in result["stderr"].strip().split("\n")[:10]:
                print(f"    {line}")
    _prompt_go_back()


def _do_uninstall():
    clear_screen()
    confirm = questionary.confirm(
        "Uninstall Kueue? This removes all queue resources and the controller.",
        default=False,
        style=custom_style,
    ).ask()
    if not confirm:
        return
    print("\n  Removing queues...")
    delete_queues()
    print("  Uninstalling Kueue controller...")
    result = uninstall_kueue()
    if result["success"]:
        print("  [OK] Kueue removed.")
    else:
        print("  [FAIL] Uninstall failed.")
        if result.get("stderr"):
            for line in result["stderr"].strip().split("\n")[:10]:
                print(f"    {line}")
    _prompt_go_back()
