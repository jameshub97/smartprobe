# Simulation Service Tool - Architecture Documentation

## Overview

The Simulation Service Tool is a CLI-based orchestration platform for managing distributed Playwright agent tests on Kubernetes. It provides an interactive terminal interface with intelligent presets, live progress tracking, diagnostics, and one-click cluster cleanup.

## Project Structure

```
simulation_service_tool.py              # Thin entry point (7 lines)
simulation_service_tool/                # Main package
    __init__.py                         # Package marker
    __main__.py                         # run() entry + CLI arg routing
    ui/                                 # UI constants and helpers
        __init__.py
        styles.py                       # questionary Style, SERVICE_URL
        utils.py                        # clear_screen()
        display.py                      # display_cleanup_result(), display_verification_result()
    services/                           # Backend communication + direct K8s operations
        __init__.py
        api_client.py                   # check_service(), call_service()
        direct_cleanup.py               # direct_*() fallbacks, get_test_releases()
    menus/                              # Dedicated menu screens
        __init__.py
        cleanup.py                      # cleanup_menu(), handle_cleanup_choice()
        presets.py                      # show_presets(), get_preset_config()
        diagnostics.py                  # diagnostics_menu()
    cli/                                # Main loop and command implementations
        __init__.py
        main.py                         # interactive_menu(), handle_menu_choice()
        commands.py                     # facade for command entry points plus preflight/snapshot/diagnostic flows
        operations.py                   # status/reset/start/stop/watch command implementations
        prompts.py                      # shared interactive prompt helpers
        watch.py                        # live watch rendering and kubectl watch helpers
        workload_guidance.py            # StatefulSet vs Job guidance shown by diagnostics
```

## Module Responsibilities

### Entry Point

| File | Purpose |
|------|---------|
| `simulation_service_tool.py` | Thin wrapper — imports and calls `run()` from the package |
| `__main__.py` | Handles CLI args (`cleanup --full/--stuck/--verify`) or launches the interactive menu |

### `ui/` — Display Layer

| File | Purpose |
|------|---------|
| `styles.py` | Shared `custom_style` for questionary menus and `SERVICE_URL` constant |
| `utils.py` | `clear_screen()` — cross-platform terminal clear |
| `display.py` | `display_cleanup_result()` and `display_verification_result()` — formatted output for cleanup/verify operations |

### `services/` — Data & Communication Layer

| File | Purpose |
|------|---------|
| `api_client.py` | `check_service()` — health check against the Flask simulation service; `call_service()` — generic REST caller for GET/POST |
| `direct_cleanup.py` | Fallback operations when the service is offline — wraps `ClusterCleanup` from `simulation_service.py` and raw `helm`/`kubectl` subprocess calls |

### `menus/` — Dedicated Menu Screens

| File | Purpose |
|------|---------|
| `cleanup.py` | Full cleanup center: reset, stuck resources, specific release, completed pods, verify, dry run |
| `presets.py` | Preset browser (tiny/small/medium/large/xlarge) with inline test launch |
| `diagnostics.py` | Diagnostics menu (placeholder) |

### `cli/` — Main Loop & Commands

| File | Purpose |
|------|---------|
| `main.py` | `interactive_menu()` — main REPL loop rendering the Agent Control Center; `handle_menu_choice()` — dispatcher |
| `commands.py` | Pure compatibility facade — re-exports every public symbol so menus can `from cli.commands import X` without knowing the underlying module |
| `operations.py` | High-level command actions: `show_status()`, `hard_reset()`, `stop_test_menu()`, `list_tests()`, `watch_progress()`, `start_service()` |
| `prompts.py` | Shared Questionary prompt helpers reused across CLI flows |
| `watch.py` | Live watch loops and direct kubectl pod watch helpers |
| `workload_guidance.py` | Diagnostic guidance for moving one-shot test agents from StatefulSets to Jobs |
| `snapshots.py` | Cluster state snapshot builders (`get_welcome_snapshot()`, `get_routine_checks_snapshot()`) and pod data helpers |
| `pod_diagnostics.py` | Interactive pod inspection flows (`diagnose_unhealthy_pod()`, `show_stale_pod_summary()`, etc.) |
| `preflight.py` | Preflight conflict detection, auto-fix, fallback logic, and the `preflight_check()` menu action |
| `start_test.py` | `start_test_menu()` and `initialize_cluster_menu()` — test launch and cluster init flows |

## Dependency Graph

```
simulation_service_tool.py
  └── __main__.py
        ├── cli/main.py
        │     ├── ui/styles.py
        │     ├── ui/utils.py
        │     ├── services/api_client.py
        │     ├── cli/commands.py
        │     │     ├── ui/styles.py
        │     │     ├── ui/utils.py
        │     │     ├── services/api_client.py
        │     │     ├── services/direct_cleanup.py
        │     │     │     └── simulation_service.ClusterCleanup
        │     │     └── menus/presets.py
        │     ├── menus/cleanup.py
        │     │     ├── ui/*
        │     │     ├── services/api_client.py
        │     │     └── services/direct_cleanup.py
        │     ├── menus/presets.py
        │     │     ├── ui/styles.py
        │     │     └── services/api_client.py
        │     └── menus/diagnostics.py
        ├── ui/display.py
        └── services/direct_cleanup.py
              └── simulation_service.ClusterCleanup
```

## Execution Modes

### Interactive (default)

```bash
python3 simulation_service_tool.py
```

Launches the full interactive menu (Agent Control Center) with questionary-based navigation.

### CLI Cleanup Commands

```bash
python3 simulation_service_tool.py cleanup --full     # Full cluster reset
python3 simulation_service_tool.py cleanup --stuck     # Clean stuck PVCs & PDBs
python3 simulation_service_tool.py cleanup --verify    # Verify cluster state
```

### As a Python Module

```bash
python3 -m simulation_service_tool
```

Runs `__main__.py` directly.

### Pass-through to Simulation Service

```bash
python3 simulation_service_tool.py <any-other-args>
```

Delegates to `simulation_service.main()` for server/status commands.

## Presets

| Name | Agents | Parallelism | Duration | Use Case |
|------|--------|-------------|----------|----------|
| `tiny` | 5 | 2 | ~10s | Quick check |
| `small` | 10 | 5 | ~30s | Dev testing |
| `medium` | 50 | 10 | ~2m | Integration |
| `large` | 100 | 20 | ~5m | Performance |
| `xlarge` | 500 | 50 | ~15m | Stress test |

## Service Communication

The tool operates in two modes depending on whether the Flask simulation service is running:

1. **Service online** — All operations go through REST calls to `http://localhost:5002` (`api_client.py`)
2. **Service offline** — Falls back to direct `helm`/`kubectl` subprocess calls and `ClusterCleanup` (`direct_cleanup.py`)

Health is checked via `GET /health` with a 2-second timeout before every menu render.

## External Dependencies

| Package | Purpose |
|---------|---------|
| `questionary` | Interactive terminal menus and prompts |
| `requests` | HTTP calls to the simulation service |
| `simulation_service` | `ClusterCleanup` class + `main()` for pass-through |
| `subprocess` | Helm and kubectl commands (fallback mode) |
