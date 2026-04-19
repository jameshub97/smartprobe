"""Compatibility facade — re-exports all CLI command entry points.

Menus import from here; implementation lives in sibling modules.
"""

# --- Pass 2 extractions ---------------------------------------------------

from simulation_service_tool.cli.operations import (  # noqa: F401
    hard_reset,
    list_tests,
    show_status,
    start_service,
    stop_test_menu,
    watch_progress,
)
from simulation_service_tool.cli.prompts import (  # noqa: F401
    _prompt_advanced_test_options,
    _prompt_go_back,
)
from simulation_service_tool.cli.watch import (  # noqa: F401
    watch_release_pods_kubectl,
)
from simulation_service_tool.cli.workload_guidance import (  # noqa: F401
    _show_job_yaml_guidance,
    _show_statefulset_keepalive_guidance,
)

# --- Pass 3 extractions ---------------------------------------------------

from simulation_service_tool.cli.snapshots import (  # noqa: F401
    get_welcome_snapshot,
    get_routine_checks_snapshot,
    _collect_release_pod_assessment,
    _get_statefulset_stale_status,
    _extract_waiting_reason,
    _format_pod_age,
    _kubectl_get_json,
    _kubectl_list_json,
    _pod_ready_value,
    _pod_restart_count,
    _pod_status_value,
)
from simulation_service_tool.cli.pod_diagnostics import (  # noqa: F401
    diagnose_unhealthy_pod,
    show_active_pods_summary,
    show_stale_pod_summary,
    show_active_ports_summary,
    view_agent_logs,
    _pick_debug_pod,
    _get_pod_logs_output,
    _get_owner_kind,
    _get_owner_name,
    _get_stale_status_for_pod,
    _print_stale_pod_details,
    _detect_statefulset_test_workload_mismatch,
    _extract_release_name_from_pod,
)
from simulation_service_tool.cli.preflight import (  # noqa: F401
    _extract_conflicting_release,
    _auto_fix_conflicts,
    _print_preflight_conflicts,
    _handle_remaining_preflight_conflicts,
    _should_fallback_to_direct,
    _fallback_info_lines,
    _pause_after_fallback,
    _get_preflight,
    _handle_start_error_recovery,
    preflight_check,
)
from simulation_service_tool.cli.start_test import (  # noqa: F401
    initialize_cluster_menu,
    start_test_menu,
)

# --- Third-party / service imports that legacy tests monkeypatch ----------

import questionary  # noqa: F401
from simulation_service_tool.ui.utils import clear_screen  # noqa: F401
from simulation_service_tool.services.command_runner import run_cli_command  # noqa: F401
from simulation_service_tool.services.direct_cleanup import (  # noqa: F401
    direct_preflight_check,
    direct_release_cleanup,
    direct_verify_state,
)
from simulation_service_tool.menus.ports import get_port_status  # noqa: F401
