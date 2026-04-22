"""Output formatting and display functions."""

import sys
import threading
import time

try:
    from rich import box
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = Console()
    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency fallback
    console = None
    RICH_AVAILABLE = False


def show_loading_spinner(loader, message="Loading cluster status...", timeout=20):
    if not callable(loader):
        raise TypeError("loader must be callable")

    if not RICH_AVAILABLE or console is None:
        return loader(progress_callback=None)

    result_holder = [None]
    exc_holder = [None]
    steps = []
    steps_lock = threading.Lock()

    def progress_callback(step_message):
        if step_message:
            with steps_lock:
                steps.append(step_message)

    def run():
        try:
            result_holder[0] = loader(progress_callback=progress_callback)
        except Exception as e:  # pragma: no cover
            exc_holder[0] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    timed_out = False
    try:
        with console.status(
            f"[bold green]{message}[/bold green]  [dim](Ctrl+C to cancel)[/dim]",
            spinner="dots",
        ):
            last_printed = 0
            start = time.monotonic()
            while thread.is_alive():
                thread.join(timeout=0.3)
                with steps_lock:
                    new_steps = steps[last_printed:]
                    last_printed = len(steps)
                for step in new_steps:
                    console.print(f"  [dim]·[/dim] {step}")
                if time.monotonic() - start >= timeout:
                    timed_out = True
                    break
            if not timed_out:
                with steps_lock:
                    for step in steps[last_printed:]:
                        console.print(f"  [dim]·[/dim] {step}")
    except KeyboardInterrupt:
        console.print("[dim]Cancelled.[/dim]")
        return None

    if timed_out:
        console.print(
            f"[yellow]  ⚠ Timed out after {timeout}s — cluster API may be unresponsive.[/yellow]"
        )
        _reset_terminal()
        return None

    _reset_terminal()
    if exc_holder[0]:
        raise exc_holder[0]
    return result_holder[0]


def _reset_terminal():
    """Flush stdout and restore cursor visibility after Rich live displays."""
    sys.stdout.write("\033[?25h")  # show cursor (Rich may have hidden it)
    sys.stdout.flush()


def render_drift_banner(banner_text, findings=None):
    """Render a drift-detection banner above the menu."""
    findings = findings or []
    warnings = [f for f in findings if f['severity'] in ('warning', 'error')]

    if not RICH_AVAILABLE or console is None:
        print("+" + "-" * 62 + "+")
        print(f"|  [DRIFT] {banner_text[:51]:<51}|")
        for finding in warnings[:3]:
            line = f"  - {finding['summary']}"[:60]
            print(f"|{line:<62}|")
        print("+" + "-" * 62 + "+")
        return

    parts = [Text(f"DRIFT DETECTED", style="bold yellow")]
    parts.append(Text())
    for finding in warnings[:4]:
        severity_style = "red" if finding['severity'] == 'error' else "yellow"
        parts.append(Text(f"  {finding['summary']}", style=severity_style))
        parts.append(Text(f"    Fix: {finding['remediation']}", style="dim"))
    if len(warnings) > 4:
        parts.append(Text(f"  ... and {len(warnings) - 4} more", style="dim"))

    console.print(Panel(
        Group(*parts),
        title="Residual Data",
        border_style="yellow",
        box=box.ROUNDED,
        padding=(1, 2),
    ))
    console.print()


def render_welcome_screen(service_running, load_overview, message="Connecting to Kubernetes..."):
    overview = show_loading_spinner(load_overview, message=message)
    if overview is None:
        overview = {}
    render_welcome_menu(service_running, overview)
    return overview


def build_smart_summary_panel(title, issues=None, recommendation=None, healthy_message=None, border_style="yellow"):
    issues = issues or []
    if not RICH_AVAILABLE or console is None:
        return None

    parts = []
    if issues:
        if recommendation:
            parts.append(Text(recommendation, style="bold yellow"))
            parts.append(Text())
        for issue in issues[:3]:
            parts.append(Text(issue.get('summary', 'Issue detected'), style="yellow"))
            parts.append(Text(f"Fix: {issue.get('remediation', 'Review the related screen for next steps.')}", style="dim"))
            parts.append(Text())
    else:
        parts.append(Text(healthy_message or "No issues detected.", style="green"))

    if parts and isinstance(parts[-1], Text) and not parts[-1].plain:
        parts.pop()

    return Panel(Group(*parts), title=title, border_style=border_style, box=box.ROUNDED, padding=(1, 2))


def render_smart_summary_panel(title, issues=None, recommendation=None, healthy_message=None, border_style="yellow"):
    issues = issues or []
    panel = build_smart_summary_panel(
        title,
        issues=issues,
        recommendation=recommendation,
        healthy_message=healthy_message,
        border_style=border_style,
    )
    if panel is not None:
        console.print(panel)
        return

    print("+" + "-" * 62 + "+")
    print(f"|  {title[:58]:<58}|")
    print("+" + "-" * 62 + "+")
    if issues:
        if recommendation:
            print(f"|  {recommendation[:58]:<58}|")
            print("|" + " " * 62 + "|")
        for issue in issues[:3]:
            summary = issue.get('summary', 'Issue detected')[:56]
            remediation = issue.get('remediation', 'Review the related screen for next steps.')[:54]
            print(f"|  - {summary:<56}|")
            print(f"|    Fix: {remediation:<51}|")
    else:
        message = (healthy_message or "No issues detected.")[:58]
        print(f"|  {message:<58}|")
    print("+" + "-" * 62 + "+")


def render_key_value_panel(title, rows, border_style="bright_blue"):
    if not RICH_AVAILABLE or console is None:
        print("+" + "=" * 62 + "+")
        print(f"|  {title[:58]:<58}|")
        print("+" + "=" * 62 + "+")
        for label, value in rows:
            line = f"{label}: {value}"[:58]
            print(f"|  {line:<58}|")
        print("+" + "=" * 62 + "+")
        return

    table = Table.grid(padding=(0, 2))
    table.add_column(style="cyan")
    table.add_column(style="white")
    for label, value in rows:
        table.add_row(str(label), str(value))
    console.print(Panel(table, title=title, border_style=border_style, box=box.ROUNDED, padding=(1, 2)))


def build_welcome_issues(service_running, overview):
    overview = overview or {}
    issues = []
    pods_pending = overview.get('pods_pending', False)
    preflight_pending = overview.get('preflight_pending', False)
    stale_pending = overview.get('stale_pending', False)
    unhealthy_pods = overview.get('unhealthy_pods', 0)
    orphaned_count = overview.get('orphaned_count', 0)
    stale_info = overview.get('stale_pod') or {}

    if not service_running:
        issues.append({
            'summary': 'simulation service is offline',
            'remediation': 'Use Start Service now, or open Diagnostics for guided recovery steps.',
        })
    if pods_pending or preflight_pending or stale_pending:
        issues.append({
            'summary': 'full Kubernetes scan has not been loaded yet',
            'remediation': 'Open Routine Checks to load pod, stale-state, and preflight details before a new run.',
        })
    if unhealthy_pods:
        issues.append({
            'summary': f'{unhealthy_pods} unhealthy pod(s) detected',
            'remediation': 'Open Routine Checks and diagnose the unhealthy pod before starting more tests.',
        })
    if orphaned_count:
        issues.append({
            'summary': f'{orphaned_count} orphaned resource conflict(s) detected',
            'remediation': 'Open Cleanup Center or run preflight cleanup in Routine Checks to clear leftovers.',
        })
    if stale_info.get('is_stale'):
        issues.append({
            'summary': f"stale pod detected: {stale_info.get('pod_name', 'playwright-agent-0')}",
            'remediation': 'Inspect the stale pod in Routine Checks before running unhealthy-pod diagnosis.',
        })

    return issues


def render_main_menu(service_running):
    if not RICH_AVAILABLE:
        service_status = "[OK] Service Running" if service_running else "[WARN] Service Offline"
        print("+" + "=" * 62 + "+")
        print("|                      TEST OPERATIONS                         |")
        print("+" + "=" * 62 + "+")
        print(f"|  {service_status:<60}|")
        print("+" + "-" * 62 + "+")
        print("|                                                              |")
        print("|  1) Start a Test                                             |")
        print("|  2) Stop a Test                                              |")
        print("|  3) List Tests                                               |")
        print("|  4) Watch Progress                                           |")
        print("|  5) Show Presets                                             |")
        print("|                                                              |")
        print("|  0) Back                                                     |")
        print("|                                                              |")
        print("+" + "=" * 62 + "+")
        return

    menu = Table.grid(padding=(0, 2))
    menu.add_column(style="bold cyan", width=3)
    menu.add_column(style="white")
    menu_rows = [
        ("1", "Start a Test"),
        ("2", "Stop a Test"),
        ("3", "List Tests"),
        ("4", "Watch Progress"),
        ("5", "Show Presets"),
        ("0", "Back"),
    ]
    for key, label in menu_rows:
        menu.add_row(key, label)

    status = Text("Service Running" if service_running else "Service Offline")
    status.stylize("bold green" if service_running else "bold yellow")
    subtitle = Text("Test actions only; routine checks live on the welcome screen", style="dim")
    content = Group(status, Text(), menu, Text(), subtitle)
    console.print(Panel(content, title="Test Operations", border_style="bright_blue", box=box.ROUNDED, padding=(1, 2)))


def render_welcome_menu(service_running, overview=None):
    overview = overview or {}
    pods_pending = overview.get('pods_pending', False)
    stale_pending = overview.get('stale_pending', False)
    preflight_pending = overview.get('preflight_pending', False)
    stale_info = overview.get('stale_pod') or {}
    active_pods = overview.get('active_pods', 0)
    healthy_pods = overview.get('healthy_pods', 0)
    unhealthy_pods = overview.get('unhealthy_pods', max(active_pods - healthy_pods, 0))
    active_ports = overview.get('active_ports', 0)
    orphaned_count = overview.get('orphaned_count', 0)
    orphaned_conflicts = overview.get('orphaned_conflicts', [])
    needs_cleanup = bool(unhealthy_pods or orphaned_count or stale_info.get('is_stale'))
    stale_status = 'not loaded yet' if stale_pending else 'stale pod detected' if stale_info.get('is_stale') else 'no stale pod detected'
    orphaned_label = 'not loaded yet' if preflight_pending else 'none detected'
    if not preflight_pending and orphaned_count:
        first_conflict = orphaned_conflicts[0]
        first_name = first_conflict.get('name', ', '.join(first_conflict.get('releases', [])))
        orphaned_label = f"{orphaned_count} ({first_conflict.get('type')}: {first_name})"
    active_pods_label = 'pending' if pods_pending else str(active_pods)
    healthy_pods_label = 'pending' if pods_pending else str(healthy_pods)

    if not RICH_AVAILABLE:

        # Minimal, clean, professional dashboard
        print("\u2554" + "\u2550" * 62 + "\u2557")
        print("\u2551{:^62}\u2551".format("AGENT CONTROL CENTER"))
        print("\u2560" + "\u2550" * 62 + "\u2563")
        print("\u2551" + " " * 62 + "\u2551")
        # Status rows
        active_ports = overview.get('active_ports', []) if isinstance(overview, dict) else []
        if isinstance(active_ports, list):
            ports_str = ', '.join(str(p.get('port', p)) for p in active_ports) if active_ports else "-"
        else:
            ports_str = str(active_ports)
        print("\u2551  Service        {:<45}\u2551".format("Running" if service_running else "Offline"))
        print("\u2551  Active pods    {:<45}\u2551".format(overview.get('active_pods', 0)))
        print("\u2551  Healthy pods   {:<45}\u2551".format(overview.get('healthy_pods', 0)))
        print("\u2551  Active ports   {:<45}\u2551".format(ports_str))
        status_label = "Clean" if overview.get('orphaned_count', 0) == 0 and overview.get('unhealthy_pods', 0) == 0 else "Needs Attention"
        print("\u2551  Status         {:<45}\u2551".format(status_label))
        print("\u2551" + " " * 62 + "\u2551")
        print("\u2570" + "\u2500" * 62 + "\u256f")
        return

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="cyan")
    summary.add_column(style="white")

    service_val = "[bold green]Running[/bold green]" if service_running else "[bold red]Offline[/bold red]"
    summary.add_row("Service", service_val)

    pods_val = "[dim]pending[/dim]" if pods_pending else (
        f"[yellow]{active_pods}[/yellow]" if active_pods == 0 else f"[green]{active_pods}[/green]"
    )
    summary.add_row("Active pods", pods_val)

    if pods_pending:
        healthy_val = "[dim]pending[/dim]"
    elif unhealthy_pods:
        healthy_val = f"[red]{healthy_pods}[/red]"
    else:
        healthy_val = f"[green]{healthy_pods}[/green]"
    summary.add_row("Healthy pods", healthy_val)

    ports_count = len(active_ports) if isinstance(active_ports, list) else int(active_ports or 0)
    ports_val = f"[cyan]{ports_count}[/cyan]" if ports_count else "[dim]0[/dim]"
    summary.add_row("Active ports", ports_val)

    if preflight_pending:
        orphaned_val = "[dim]not loaded yet[/dim]"
    elif orphaned_count:
        orphaned_val = f"[yellow]{orphaned_label}[/yellow]"
    else:
        orphaned_val = "[green]none detected[/green]"
    summary.add_row("Orphaned", orphaned_val)

    if stale_pending:
        stale_val = "[dim]not loaded yet[/dim]"
    elif stale_info.get('is_stale'):
        stale_val = "[yellow]stale pod detected[/yellow]"
    else:
        stale_val = "[green]no stale pod detected[/green]"
    summary.add_row("Stale pod", stale_val)

    subtitle = Text("Run Routine Checks for a full cluster scan before entering test actions", style="dim")
    warning = None
    if unhealthy_pods:
        warning = Text(f"WARNING: {unhealthy_pods} unhealthy pod(s) detected", style="bold yellow")
        subtitle = Text("Recommended: Cleanup Center first; use Hard Reset when you need a full wipe", style="dim")
    elif needs_cleanup:
        subtitle = Text("Recommended: open Cleanup Center before starting another test", style="dim")
    elif pods_pending or preflight_pending or stale_pending:
        subtitle = Text("Startup uses a fast local snapshot. Open Routine Checks to load Kubernetes state.", style="dim")

    content_parts = [summary, Text()]
    content_parts.extend([warning if warning else Text(), Text(), subtitle])
    content = Group(*content_parts)
    console.print(Panel(content, title="Welcome", border_style="bright_blue", box=box.ROUNDED, padding=(1, 2)))


def render_routine_checks_dashboard(snapshot):
    pods = snapshot.get('pods', [])
    unhealthy_pods = snapshot.get('unhealthy_pods', [])
    pods_pending = snapshot.get('pods_pending', False)
    pod_error = snapshot.get('pod_error')
    active_ports = snapshot.get('active_ports', [])
    conflicts = snapshot.get('preflight_conflicts', [])
    preflight_pending = snapshot.get('preflight_pending', False)
    stale_pending = snapshot.get('stale_pending', False)
    stale_pod = snapshot.get('stale_pod') or {}

    if not RICH_AVAILABLE:
        print("+" + "=" * 62 + "+")
        print("|                      ROUTINE CHECKS                          |")
        print("+" + "=" * 62 + "+")
        print("\n  POD DETAILS")
        for pod in pods[:5]:
            print(f"   {pod['name']} | {pod['ready']} | {pod['status']} | restarts={pod['restarts']} | age={pod['age']}")
        if pods_pending:
            print("   Not loaded yet")
            print("   Use 'Refresh' to scan Kubernetes pod state.")
        elif pod_error:
            print(f"   {pod_error}")
        elif not pods:
            print("   None detected")
        if unhealthy_pods:
            print("\n  [WARN] Unhealthy pod detected. Diagnose it before test operations.")

        print("\n  ACTIVE PORTS")
        for port in active_ports:
            print(f"   {port['port']}  {port['service']}")
        if not active_ports:
            print("   None detected")

        print("\n  ORPHANED RESOURCES")
        if preflight_pending:
            print("   Not loaded yet")
            print("   Use 'Run preflight cleanup' to scan cluster conflicts.")
        elif conflicts:
            for conflict in conflicts:
                name = conflict.get('name', ', '.join(conflict.get('releases', [])))
                print(f"   {conflict.get('type')}: {name}")
        else:
            print("   None detected")

        if stale_pending:
            print("\n  Stale pod check not loaded yet")
            print("   Refresh later after the first screen if you need stale-state details.")
        elif stale_pod.get('is_stale'):
            print(f"\n  [WARN] Stale pod detected: {stale_pod.get('pod_name')}")
        return

    pod_table = Table(box=box.SIMPLE_HEAVY)
    pod_table.add_column("Name", style="cyan")
    pod_table.add_column("Ready", justify="right")
    pod_table.add_column("Status")
    pod_table.add_column("Restarts", justify="right")
    pod_table.add_column("Age", justify="right")
    for pod in pods[:8]:
        status_style = "red" if pod in unhealthy_pods else "green"
        pod_table.add_row(pod['name'], pod['ready'], f"[{status_style}]{pod['status']}[/{status_style}]", str(pod['restarts']), pod['age'])
    if pods_pending:
        pod_table.add_row("pending", "-", "Use Refresh to scan pod state", "-", "-")
    elif pod_error:
        pod_table.add_row("error", "-", pod_error, "-", "-")
    elif not pods:
        pod_table.add_row("None detected", "-", "-", "-", "-")

    ports_table = Table(box=box.SIMPLE_HEAVY)
    ports_table.add_column("Port", style="cyan")
    ports_table.add_column("Service")
    for port in active_ports:
        ports_table.add_row(port['port'], port['service'])
    if not active_ports:
        ports_table.add_row("-", "None detected")

    conflicts_table = Table(box=box.SIMPLE_HEAVY)
    conflicts_table.add_column("Type", style="cyan")
    conflicts_table.add_column("Resource")
    for conflict in conflicts[:6]:
        name = conflict.get('name', ', '.join(conflict.get('releases', [])))
        conflicts_table.add_row(conflict.get('type', 'unknown'), name or 'unknown')
    if preflight_pending:
        conflicts_table.add_row("pending", "Run preflight cleanup to scan conflicts")
    elif not conflicts:
        conflicts_table.add_row("none", "None detected")

    guidance = []
    if unhealthy_pods:
        guidance.append(Text("WARNING: Unhealthy pod detected. Diagnose it before entering test operations.", style="bold yellow"))
    if stale_pending:
        guidance.append(Text("Stale pod check not loaded yet. Use Refresh after the first render if needed.", style="yellow"))
    elif stale_pod.get('is_stale'):
        guidance.append(Text(f"Stale pod detected: {stale_pod.get('pod_name')}", style="bold yellow"))
    if not guidance:
        guidance.append(Text("Cluster checks look routine. You can proceed to test operations when ready.", style="green"))

    content = Group(
        Text("Pod Details", style="bold"),
        pod_table,
        Text(),
        Text("Active Ports", style="bold"),
        ports_table,
        Text(),
        Text("Orphaned Resources", style="bold"),
        conflicts_table,
        Text(),
        *guidance,
    )
    console.print(Panel(content, title="Routine Checks", border_style="bright_blue", box=box.ROUNDED, padding=(1, 2)))


def render_status_summary(service_running, payload):
    if not RICH_AVAILABLE:
        if service_running:
            if 'error' not in payload:
                print(f"\n[STATUS] {payload.get('total', 0)} total | OK: {payload.get('success', 0)} | Running: {payload.get('running', 0)}")
                act = payload.get('activation', {})
                if act.get('count'):
                    print(f"   Activation: avg {act['avg']}s | p95 {act['p95']}s ({act['count']} pods)")
                tp = payload.get('throughput', {})
                if tp.get('completed'):
                    print(f"   Throughput: {tp['agentsPerSecond']}/s | {tp['percentComplete']}% complete | ETA {tp['etaSeconds']}s")
            else:
                print(f"\n[ERROR] {payload['error']}")
        else:
            print("\n[STATUS] Direct Status:")
            print(f"   Test releases: {payload['helm_test_releases']}")
            print(f"   Playwright pods: {payload['playwright_pods']}")
        return

    if service_running and 'error' in payload:
        console.print(Panel(payload['error'], title="Service Error", border_style="red", box=box.ROUNDED))
        return

    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    if service_running:
        table.add_row("Total", str(payload.get('total', 0)))
        table.add_row("Succeeded", f"[green]{payload.get('success', 0)}[/green]")
        table.add_row("Running", f"[yellow]{payload.get('running', 0)}[/yellow]")
        table.add_row("Errors", f"[red]{payload.get('errors', 0)}[/red]")
        table.add_row("Pending", str(payload.get('pending', 0)))
        act = payload.get('activation', {})
        if act.get('count'):
            table.add_row("Activation (avg)", f"[yellow]{act['avg']}s[/yellow]")
            table.add_row("Activation (p95)", f"[yellow]{act['p95']}s[/yellow]")
        tp = payload.get('throughput', {})
        if tp.get('completed'):
            table.add_row("Throughput", f"[bold cyan]{tp['agentsPerSecond']}/s[/bold cyan]")
            table.add_row("Complete", f"[bold green]{tp['percentComplete']}%[/bold green]")
            if tp.get('avgDuration') is not None:
                table.add_row("Avg Duration", f"[yellow]{tp['avgDuration']}s[/yellow]")
            eta = tp['etaSeconds']
            eta_str = f"{round(eta)}s" if eta < 60 else f"{int(eta // 60)}m {round(eta % 60)}s"
            table.add_row("ETA", f"[cyan]{eta_str}[/cyan]" if eta > 0 else "[green]done[/green]")
        title = "Simulation Summary"
    else:
        table.add_row("Test releases", str(payload.get('helm_test_releases', '?')))
        table.add_row("Playwright pods", str(payload.get('playwright_pods', '?')))
        table.add_row("Stuck PVCs", str(payload.get('playwright_pvcs', '?')))
        table.add_row("Conflicting PDBs", str(payload.get('conflicting_pdbs', '?')))
        title = "Direct Cluster Status"
    console.print(Panel(table, title=title, border_style="bright_blue", box=box.ROUNDED, padding=(1, 2)))


def display_cleanup_result(result, dry_run):
    if dry_run:
        print("\n[DRY RUN] Would delete:")
    else:
        print("\n[OK] Cleanup completed:")
    if isinstance(result, dict):
        if 'quick_cleanup' in result:
            commands = result['quick_cleanup'].get('commands', [])
            resources = result['quick_cleanup'].get('resources', [])
            if commands and dry_run:
                print("   Quick clean commands:")
                for command in commands:
                    print(f"      {command}")
            if resources:
                print(f"   Quick-clean resources: {', '.join(resources)}")
            if result.get('errors'):
                print("   Quick-clean warnings:")
                for error in result['errors']:
                    print(f"      {error}")
        if 'helm_releases' in result:
            releases = result['helm_releases'].get('releases', [])
            if releases:
                print(f"   Releases: {', '.join(releases)}")
        if 'stuck_resources' in result:
            resources = result['stuck_resources'].get('resources', [])
            if resources:
                print(f"   Resources: {', '.join(resources)}")
        if 'orphaned_pvcs' in result:
            pvcs = result['orphaned_pvcs'].get('pvcs', [])
            if pvcs:
                print(f"   PVCs: {', '.join(pvcs)}")
        if 'conflicting_pdbs' in result:
            pdbs = result['conflicting_pdbs'].get('pdbs', [])
            if pdbs:
                print(f"   PDBs: {', '.join(pdbs)}")
        if 'pods' in result:
            if result['pods']:
                print(f"   Pods: {len(result['pods'])} deleted")


def display_verification_result(result):
    print("\n[STATUS] Cluster State:")
    if 'state' in result:
        state = result['state']
    else:
        state = result
    print(f"   Test releases: {state.get('helm_test_releases', '?')}")
    print(f"   Playwright pods: {state.get('playwright_pods', '?')}")
    print(f"   Stuck PVCs: {state.get('playwright_pvcs', '?')}")
    print(f"   Conflicting PDBs: {state.get('conflicting_pdbs', '?')}")
    if state.get('is_clean', False):
        print("\n   [OK] Cluster is CLEAN and ready!")
    else:
        print("\n   [WARN] Cluster has leftover resources")
