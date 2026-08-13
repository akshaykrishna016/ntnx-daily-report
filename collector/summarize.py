"""Build the estate-wide ``Summary`` used by the KPI strip, gauges and body.

Rolls the collected clusters, hosts and VMs into the aggregate figures the
executive view needs. Kept separate from collection so the aggregation is easy
to read and reason about.
"""

import logging

import units

LOG = logging.getLogger("ntnx.summary")

# Efficiency labels shown as tiles, in mockup order.
EFFICIENCY_TILE_ORDER = ["Inactive", "Overprovisioned", "Constrained", "Bully"]


def build_summary(
    clusters,
    hosts,
    vms,
    eff_map,
    eff_available,
    critical_alert_count,
    storage_runway_days,
    vm_stats_failures,
):
    """Construct a ``Summary`` from the collected entities and KPI values.

    Args:
        clusters: List of ``Cluster`` records (AOS only).
        hosts: List of ``Host`` records.
        vms: List of ``VM`` records (in-scope only).
        eff_map: Map of VM name -> canonical efficiency status.
        eff_available: ``False`` when the efficiency call failed (tiles N/A).
        critical_alert_count: Int or ``None``.
        storage_runway_days: Int or ``None``.
        vm_stats_failures: Count of VMs whose stats failed.

    Returns:
        A ``collector.model.Summary`` instance.
    """
    from collector.model import Summary

    # ---- Aggregate CPU capacity vs. consumed --------------------------------
    cpu_capacity_hz = sum(c.cpu_capacity_hz for c in clusters)
    cpu_used_hz = sum(
        c.cpu_capacity_hz * (c.cpu_avg_pct / 100.0) for c in clusters
    )
    cpu_pct = _safe_pct(cpu_used_hz, cpu_capacity_hz)

    # ---- Aggregate memory ---------------------------------------------------
    mem_capacity_gib = sum(c.mem_capacity_gib for c in clusters)
    mem_used_gib = sum(c.mem_avg_gib for c in clusters)
    mem_pct = _safe_pct(mem_used_gib, mem_capacity_gib)

    # ---- Aggregate storage --------------------------------------------------
    storage_total_bytes = sum(c.storage_total_bytes for c in clusters)
    storage_used_bytes = sum(c.storage_used_bytes for c in clusters)
    storage_pct = _safe_pct(storage_used_bytes, storage_total_bytes)

    # ---- VM power counts ----------------------------------------------------
    vm_total = len(vms)
    vm_on = sum(1 for vm in vms if str(vm.power_state).upper() == "ON")
    vm_off = vm_total - vm_on

    # ---- Efficiency tile counts --------------------------------------------
    # eff_map values are lists (a VM can hold more than one status), so each
    # status is counted toward its tile.
    efficiency_counts = {label: 0 for label in units.EFFICIENCY_CANONICAL}
    for statuses in eff_map.values():
        for status in statuses:
            if status in efficiency_counts:
                efficiency_counts[status] += 1

    summary = Summary(
        cluster_count=len(clusters),
        clusters_healthy=len(clusters),
        host_count=len(hosts),
        vm_total=vm_total,
        vm_on=vm_on,
        vm_off=vm_off,
        cpu_capacity_hz=cpu_capacity_hz,
        cpu_used_hz=cpu_used_hz,
        cpu_pct=cpu_pct,
        mem_capacity_gib=mem_capacity_gib,
        mem_used_gib=mem_used_gib,
        mem_pct=mem_pct,
        storage_total_bytes=storage_total_bytes,
        storage_used_bytes=storage_used_bytes,
        storage_pct=storage_pct,
        efficiency_counts=efficiency_counts,
        efficiency_available=eff_available,
        critical_alert_count=critical_alert_count,
        storage_runway_days=storage_runway_days,
        vm_stats_failures=vm_stats_failures,
    )
    LOG.info(
        "Summary: %d clusters, %d hosts, %d VMs (%d on / %d off)",
        summary.cluster_count,
        summary.host_count,
        summary.vm_total,
        summary.vm_on,
        summary.vm_off,
    )
    return summary


def _safe_pct(used, total):
    """Return used/total as a percentage, or 0.0 when total is zero."""
    if not total:
        return 0.0
    return float(used) / float(total) * 100.0
