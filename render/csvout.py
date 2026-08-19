"""CSV inventory writer for the Nutanix daily report.

Produces ``vm_inventory_{YYYYMMDD}.csv`` containing every VM in the estate (not
just the top 25 shown inline), with the same columns as table C plus the
cluster name and power state (design document, section 8).

The row-building function ``vm_to_csv_row`` is a pure function so it can be
unit-tested without touching the filesystem.
"""

import csv

import units

# Column order for the CSV. Table C columns plus cluster name and power state.
CSV_HEADER = [
    "VM Name",
    "Cluster",
    "Power State",
    "vCPU",
    "CPU Avg %",
    "CPU MAX %",
    "Mem (GiB)",
    "Mem Used (GiB)",
    "Mem MAX (GiB)",
    "Storage",
    "Storage Used",
    "Storage Free",
    "Disks",
    "Efficiency",
]

# Em dash used consistently for "no data" cells.
EM_DASH = "—"


def _pct_or_dash(value, stats_ok):
    """Return a rounded percent string, or an em dash when stats are missing."""
    if not stats_ok:
        return EM_DASH
    return str(units.pct_round(value))


def vm_to_csv_row(vm):
    """Build one CSV row (a list of strings) from a VM record.

    Args:
        vm: A ``collector.model.VM`` instance.

    Returns:
        A list of string cells in ``CSV_HEADER`` order.
    """
    guest_used = (
        units.fmt_storage(vm.guest_used_bytes)
        if vm.guest_used_bytes is not None
        else EM_DASH
    )
    guest_free = (
        units.fmt_storage(vm.guest_free_bytes)
        if vm.guest_free_bytes is not None
        else EM_DASH
    )
    return [
        vm.name,
        vm.cluster_name,
        vm.power_state,
        str(vm.vcpus),
        _pct_or_dash(vm.cpu_avg_pct, vm.stats_ok),
        _pct_or_dash(vm.cpu_max_pct, vm.stats_ok),
        units.fmt_int(vm.mem_capacity_gib),
        units.fmt_int(vm.mem_avg_gib) if vm.stats_ok else EM_DASH,
        units.fmt_int(vm.mem_max_gib) if vm.stats_ok else EM_DASH,
        units.fmt_storage(vm.storage_total_bytes),
        guest_used,
        guest_free,
        str(vm.disk_count),
        vm.efficiency_status if vm.efficiency_status else EM_DASH,
    ]


def write_vm_inventory_csv(path, vms):
    """Write the full VM inventory to a CSV file at ``path``.

    Args:
        path: Destination file path.
        vms: Iterable of ``collector.model.VM`` records (all VMs, not top-N).

    Returns:
        The number of data rows written.
    """
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        count = 0
        for vm in vms:
            writer.writerow(vm_to_csv_row(vm))
            count += 1
    return count
