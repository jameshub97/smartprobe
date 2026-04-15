"""Helpers for managing Kueue workload queuing on Kubernetes."""

from __future__ import annotations

import json

from simulation_service_tool.services.command_runner import run_cli_command

KUEUE_MANIFEST = "k8s/kueue-queues.yaml"
KUEUE_VERSION = "v0.17.0"
KUEUE_INSTALL_URL = (
    f"https://github.com/kubernetes-sigs/kueue/releases/download/{KUEUE_VERSION}"
    f"/manifests.yaml"
)


# ── Installation ───────────────────────────────────────────────────

def is_kueue_installed() -> bool:
    """Check whether the Kueue CRDs exist on the cluster."""
    result = run_cli_command(
        ["kubectl", "get", "crd", "clusterqueues.kueue.x-k8s.io"],
    )
    return result.returncode == 0


def install_kueue() -> dict:
    """Install Kueue from the upstream release manifest."""
    result = run_cli_command(
        ["kubectl", "apply", "--server-side", "-f", KUEUE_INSTALL_URL],
    )
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def uninstall_kueue() -> dict:
    """Remove Kueue from the cluster."""
    result = run_cli_command(
        ["kubectl", "delete", "-f", KUEUE_INSTALL_URL, "--ignore-not-found"],
    )
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


# ── Queue management ──────────────────────────────────────────────

def apply_queues(manifest: str = KUEUE_MANIFEST) -> dict:
    """Apply the ResourceFlavor, ClusterQueue, and LocalQueue manifests."""
    result = run_cli_command(["kubectl", "apply", "-f", manifest])
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def delete_queues(manifest: str = KUEUE_MANIFEST) -> dict:
    """Remove all Kueue queue resources."""
    result = run_cli_command(
        ["kubectl", "delete", "-f", manifest, "--ignore-not-found"],
    )
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


# ── Status ────────────────────────────────────────────────────────

def get_cluster_queue_status() -> dict:
    """Return the status of the simulation ClusterQueue."""
    result = run_cli_command(
        ["kubectl", "get", "clusterqueue", "simulation-cluster-queue",
         "-o", "json"],
    )
    if result.returncode != 0:
        return {"exists": False}
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return {"exists": False}

    status = data.get("status", {})
    spec = data.get("spec", {})

    # Extract nominal quota from first resource group
    quotas = {}
    for rg in spec.get("resourceGroups", []):
        for flavor in rg.get("flavors", []):
            for res in flavor.get("resources", []):
                quotas[res["name"]] = res.get("nominalQuota", "?")

    return {
        "exists": True,
        "pending_workloads": status.get("pendingWorkloads", 0),
        "admitted_workloads": status.get("admittedWorkloads", 0),
        "quotas": quotas,
    }


def get_local_queue_status() -> dict:
    """Return the status of the simulation LocalQueue."""
    result = run_cli_command(
        ["kubectl", "get", "localqueue", "simulation-queue", "-o", "json"],
    )
    if result.returncode != 0:
        return {"exists": False}
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return {"exists": False}

    status = data.get("status", {})
    return {
        "exists": True,
        "pending_workloads": status.get("pendingWorkloads", 0),
        "admitted_workloads": status.get("admittedWorkloads", 0),
    }


def list_workloads() -> list[dict]:
    """List Kueue workloads in the default namespace."""
    result = run_cli_command(
        ["kubectl", "get", "workloads", "-o", "json"],
    )
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return []

    workloads = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        conditions = status.get("conditions", [])
        # Find the Admitted condition
        admitted = False
        for c in conditions:
            if c.get("type") == "Admitted" and c.get("status") == "True":
                admitted = True
                break
        workloads.append({
            "name": meta.get("name", ""),
            "queue": meta.get("labels", {}).get(
                "kueue.x-k8s.io/queue-name", ""),
            "admitted": admitted,
            "created": meta.get("creationTimestamp", ""),
        })
    return workloads
