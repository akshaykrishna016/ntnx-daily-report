"""VM inventory, per-VM stats, and merge of guest-storage / efficiency data.

Collects the AHV VM inventory, excludes CVMs and the Prism Central VM, then
fetches each VM's usage stats. Stats calls run on a small thread pool (max 8
workers) so a few hundred VMs finish well inside the five-minute budget
(design document, section 5.3). Every per-VM stats call is guarded: a single
VM's failure marks that VM's row as degraded and increments a failure counter,
but never aborts the run (section 11).

Guest Used/Free storage (NGT-sourced) and the efficiency status are merged in
from maps the caller collected once for the whole estate.
"""

import fnmatch
import logging
from concurrent.futures import ThreadPoolExecutor

import units
from collector import metric_names
from collector.model import VM
from collector.parsing import extract_samples

LOG = logging.getLogger("ntnx.vms")

# Default cap on concurrent stats calls. Kept well below the Prism Central
# global rate limit (30 req/s on the smallest PC) to avoid HTTP 429 storms;
# overridable via config ``report.stats_workers``.
DEFAULT_STATS_WORKERS = 4


def collect_vms(client, cluster_name_by_id, window, config, guest_map, eff_map):
    """Collect all in-scope VMs as ``VM`` records.

    Args:
        client: A PrismClient or MockClient.
        cluster_name_by_id: Map of cluster extId -> cluster name (only in-scope
            AOS clusters; VMs on other clusters, e.g. the PC entity, are
            dropped).
        window: Dict with stats query ``params``.
        config: The parsed config dict (for ``exclude_vm_patterns``).
        guest_map: Map of VM name -> (guest_used_bytes, guest_free_bytes).
        eff_map: Map of VM name -> canonical efficiency status.

    Returns:
        A tuple ``(vms, stats_failure_count)``.
    """
    raw_vms = client.paginate_v4("/api/vmm/v4.0/ahv/config/vms")
    LOG.info("VM inventory returned %d entities", len(raw_vms))

    exclude_patterns = _exclude_patterns(config)

    # Build the base records (inventory only) for the in-scope VMs.
    base_records = []
    for raw in raw_vms:
        if _is_excluded(raw, exclude_patterns):
            continue
        cluster_ext_id = _owning_cluster_ext_id(raw)
        if cluster_ext_id not in cluster_name_by_id:
            # VM belongs to a cluster we are not reporting on (e.g. PC).
            continue
        base_records.append((raw, cluster_name_by_id[cluster_ext_id]))

    LOG.info("In-scope VMs after exclusion: %d", len(base_records))

    # Fetch stats concurrently, keyed by extId.
    workers = (config.get("report") or {}).get(
        "stats_workers", DEFAULT_STATS_WORKERS
    )
    ext_ids = [raw.get("extId") for (raw, _name) in base_records]
    stats_by_ext_id, failure_count = _fetch_all_stats(
        client, ext_ids, window, workers
    )

    vms = []
    for (raw, cluster_name) in base_records:
        vms.append(
            _build_vm(raw, cluster_name, stats_by_ext_id, guest_map, eff_map)
        )

    if failure_count:
        LOG.warning(
            "%d VM(s) could not be fully collected (stats failed)",
            failure_count,
        )
    return vms, failure_count


def _exclude_patterns(config):
    """Return the configured VM-name exclusion glob patterns."""
    report_cfg = config.get("report") or {}
    return report_cfg.get("exclude_vm_patterns") or []


def _is_excluded(raw, exclude_patterns):
    """Decide whether a raw VM should be excluded from all VM views.

    Excludes the Prism Central VM (v4 marks it with ``machineType == "PC"``),
    Controller VMs / agent VMs where those flags exist, and any VM whose name
    matches a configured exclusion glob (e.g. ``NTNX-*-CVM``).
    """
    if raw.get("machineType") == "PC":
        return True
    if raw.get("isCvm"):
        return True
    if raw.get("isAgentVm"):
        return True
    name = raw.get("name") or ""
    for pattern in exclude_patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def _owning_cluster_ext_id(raw):
    """Extract the owning cluster's extId from a raw VM record."""
    cluster = raw.get("cluster") or {}
    if isinstance(cluster, dict):
        return cluster.get("extId")
    return None


def _fetch_all_stats(client, ext_ids, window, workers=DEFAULT_STATS_WORKERS):
    """Fetch stats for many VMs concurrently.

    Args:
        client: A PrismClient or MockClient.
        ext_ids: List of VM extIds to fetch stats for.
        window: Dict with stats query ``params``.
        workers: Maximum concurrent stats calls.

    Returns:
        A tuple ``(stats_by_ext_id, failure_count)`` where ``stats_by_ext_id``
        maps extId -> raw stats payload for VMs that succeeded, and
        ``failure_count`` is the number that failed.
    """
    stats_by_ext_id = {}
    failure_count = 0

    params = dict(window.get("params") or {})
    params["$select"] = metric_names.VM_STATS_SELECT

    def fetch_one(ext_id):
        """Fetch one VM's stats; return (ext_id, payload_or_None)."""
        path = "/api/vmm/v4.0/ahv/stats/vms/{vid}".format(vid=ext_id)
        try:
            payload = client.get_json(path, params=params)
            return ext_id, payload
        except Exception as exc:
            LOG.warning("VM %s: stats call failed (%s); row degraded",
                        ext_id, exc)
            return ext_id, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for ext_id, payload in pool.map(fetch_one, ext_ids):
            if payload is None:
                failure_count += 1
            else:
                stats_by_ext_id[ext_id] = payload

    return stats_by_ext_id, failure_count


def _build_vm(raw, cluster_name, stats_by_ext_id, guest_map, eff_map):
    """Build one ``VM`` from inventory plus merged stats / guest / efficiency."""
    ext_id = raw.get("extId")
    name = raw.get("name") or ext_id

    sockets = raw.get("numSockets") or 0
    cores_per_socket = raw.get("numCoresPerSocket") or 0
    threads_per_core = raw.get("numThreadsPerCore") or 1
    vcpus = sockets * cores_per_socket * threads_per_core

    mem_capacity_gib = units.bytes_to_gib(raw.get("memorySizeBytes") or 0)

    disks = raw.get("disks") or []
    disk_count = len(disks)
    # v4 nests the disk size under backingInfo: disks[].backingInfo.diskSizeBytes.
    storage_total_bytes = 0
    for disk in disks:
        backing = disk.get("backingInfo") or {}
        storage_total_bytes += backing.get("diskSizeBytes") or 0

    power_state = raw.get("powerState") or "UNKNOWN"

    # Stats (may be missing for a failed VM).
    stats_ok = ext_id in stats_by_ext_id
    cpu_avg_pct = 0.0
    cpu_max_pct = 0.0
    mem_avg_gib = 0.0
    mem_max_gib = 0.0
    if stats_ok:
        payload = stats_by_ext_id[ext_id]
        cpu_samples = extract_samples(payload, metric_names.VM_CPU_USAGE_PPM)
        cpu_avg_pct = units.ppm_to_pct(units.aggregate_avg(cpu_samples))
        cpu_max_pct = units.ppm_to_pct(units.aggregate_max(cpu_samples))

        mem_samples = extract_samples(payload, metric_names.VM_MEM_USAGE_PPM)
        if not mem_samples:
            # Try the alternate memory metric name seen on some AOS versions.
            mem_samples = extract_samples(
                payload, metric_names.VM_MEM_USAGE_PPM_ALT
            )
        mem_avg_gib = units.mem_used_gib(
            mem_capacity_gib, units.aggregate_avg(mem_samples)
        )
        mem_max_gib = units.mem_used_gib(
            mem_capacity_gib, units.aggregate_max(mem_samples)
        )

    # Guest storage (NGT); absent -> None -> em dash in the report.
    guest_used_bytes = None
    guest_free_bytes = None
    if name in guest_map:
        guest_used_bytes, guest_free_bytes = guest_map[name]

    # eff_map holds a list of statuses per VM; join for display (or None).
    efficiency_labels = eff_map.get(name)
    efficiency_status = (
        ", ".join(efficiency_labels) if efficiency_labels else None
    )

    return VM(
        name=name,
        cluster_name=cluster_name,
        vcpus=vcpus,
        cpu_avg_pct=cpu_avg_pct,
        cpu_max_pct=cpu_max_pct,
        mem_capacity_gib=mem_capacity_gib,
        mem_avg_gib=mem_avg_gib,
        mem_max_gib=mem_max_gib,
        storage_total_bytes=storage_total_bytes,
        guest_used_bytes=guest_used_bytes,
        guest_free_bytes=guest_free_bytes,
        disk_count=disk_count,
        power_state=power_state,
        efficiency_status=efficiency_status,
        stats_ok=stats_ok,
    )
