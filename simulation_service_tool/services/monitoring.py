"""Helpers for managing the kube-prometheus-stack monitoring deployment."""

from __future__ import annotations

from simulation_service_tool.services.command_runner import run_cli_command

HELM_REPO_NAME = "prometheus-community"
HELM_REPO_URL = "https://prometheus-community.github.io/helm-charts"
CHART_NAME = "kube-prometheus-stack"
RELEASE_NAME = "monitoring"
NAMESPACE = "monitoring"
VALUES_FILE = "helm/monitoring/values.yaml"


def is_helm_available() -> bool:
    result = run_cli_command(["helm", "version", "--short"])
    return result.returncode == 0


def is_monitoring_installed() -> bool:
    result = run_cli_command(
        ["helm", "status", RELEASE_NAME],
        namespace=NAMESPACE,
    )
    return result.returncode == 0


def install_stack(values_file: str = VALUES_FILE) -> dict:
    """Install kube-prometheus-stack via Helm."""
    # Ensure repo exists
    run_cli_command(["helm", "repo", "add", HELM_REPO_NAME, HELM_REPO_URL])
    run_cli_command(["helm", "repo", "update"])

    cmd = [
        "helm", "install", RELEASE_NAME,
        f"{HELM_REPO_NAME}/{CHART_NAME}",
        "-f", values_file,
        "--namespace", NAMESPACE,
        "--create-namespace",
        "--wait",
        "--timeout", "5m",
    ]
    result = run_cli_command(cmd)
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def upgrade_stack(values_file: str = VALUES_FILE) -> dict:
    """Upgrade (or install) the monitoring stack."""
    run_cli_command(["helm", "repo", "update"])
    cmd = [
        "helm", "upgrade", "--install", RELEASE_NAME,
        f"{HELM_REPO_NAME}/{CHART_NAME}",
        "-f", values_file,
        "--namespace", NAMESPACE,
        "--create-namespace",
        "--wait",
        "--timeout", "5m",
    ]
    result = run_cli_command(cmd)
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def uninstall_stack() -> dict:
    """Uninstall the monitoring stack."""
    result = run_cli_command(
        ["helm", "uninstall", RELEASE_NAME, "--namespace", NAMESPACE],
    )
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def get_stack_status() -> dict:
    """Return high-level status of the monitoring stack."""
    result = run_cli_command(
        ["helm", "status", RELEASE_NAME, "-o", "json"],
        namespace=NAMESPACE,
    )
    if result.returncode != 0:
        return {"installed": False}

    import json
    try:
        info = json.loads(result.stdout)
    except Exception:
        info = {}

    return {
        "installed": True,
        "status": info.get("info", {}).get("status", "unknown"),
        "version": info.get("version", ""),
        "namespace": NAMESPACE,
    }


def get_prometheus_targets() -> dict:
    """Query Prometheus /api/v1/targets via kubectl port-forward output."""
    # This returns a kubectl command the user can run
    return {
        "command": (
            f"kubectl port-forward -n {NAMESPACE} "
            f"svc/{RELEASE_NAME}-prometheus 9090:9090"
        ),
        "url": "http://localhost:9090/targets",
    }


def get_grafana_access() -> dict:
    """Return the command to port-forward Grafana."""
    return {
        "command": (
            f"kubectl port-forward -n {NAMESPACE} "
            f"svc/{RELEASE_NAME}-grafana 3001:3000"
        ),
        "url": "http://localhost:3001",
        "credentials": {"username": "admin", "password": "admin"},
    }


def get_monitoring_pods() -> list[dict]:
    """List pods in the monitoring namespace."""
    result = run_cli_command(
        ["kubectl", "get", "pods", "-n", NAMESPACE, "--no-headers"],
    )
    pods = []
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 3:
                pods.append({
                    "name": parts[0],
                    "ready": parts[1],
                    "status": parts[2],
                })
    return pods


def apply_servicemonitor() -> dict:
    """Apply the ServiceMonitor and PrometheusRule manifests."""
    results = []
    for manifest in (
        "k8s/monitoring-servicemonitor.yaml",
        "k8s/monitoring-alerts.yaml",
        "k8s/simulation-service.yaml",
    ):
        r = run_cli_command(["kubectl", "apply", "-f", manifest])
        results.append({
            "manifest": manifest,
            "success": r.returncode == 0,
            "output": r.stdout or r.stderr,
        })
    return {"results": results, "success": all(r["success"] for r in results)}
