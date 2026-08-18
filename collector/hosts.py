"""Host inventory and stats collection.

Fetches the hypervisor hosts belonging to one AOS cluster and, for each host,
aggregates its CPU / memory / storage usage over the reporting window. Every
per-host stats call is wrapped individually so that one host's stats failure
degrades that single row rather than aborting the report (design document,
section 11).
"""

import logging

import units
from collector import metric_names
from collector.model import Host
from collector.parsing import extract_samples, latest_sample

LOG = logging.getLogger("ntnx.hosts")


def collect_hosts(client, cluster_ext_id, cluster_name, window):
    """Collect all hosts for one cluster as ``Host`` records.

    Args:
        client: A PrismClient or MockClient.
        cluster_ext_id: The owning cluster's extId.
        cluster_name: The owning cluster's display name.
        window: Dict with ``params`` for the stats query (start/end/interval).

    Returns:
        A list of ``Host`` records (one per host, always populated even if that
        host's stats call failed — failed stats show as zeros/None and a
        warning is logged).
    """
    path = "/api/clustermgmt/v4.0/config/clusters/{cid}/hosts".format(
        cid=cluster_ext_id
    )
    raw_hosts = client.paginate_v4(path)
    LOG.info(
        "Cluster %s: %d hosts in inventory", cluster_name, len(raw_hosts)
    )

    hosts = []
    for raw in raw_hosts:
        hosts.append(
            _build_host(client, cluster_ext_id, cluster_name, raw, window)
        )
    return hosts


def _build_host(client, cluster_ext_id, cluster_name, raw, window):
    """Build one ``Host`` from inventory plus a guarded stats call."""
    host_ext_id = raw.get("extId")
    name = raw.get("hostName") or raw.get("name") or host_ext_id

    cores = raw.get("numberOfCpuCores") or 0
    frequency_hz = raw.get("cpuFrequencyHz") or 0
    cpu_capacity_hz = cores * frequency_hz

    mem_capacity_bytes = raw.get("memorySizeBytes") or 0
    mem_capacity_gib = units.bytes_to_gib(mem_capacity_bytes)

    ip = ""
    hypervisor = raw.get("hypervisor") or {}
    if isinstance(hypervisor, dict):
        ip = hypervisor.get("ipAddress") or ""

    # Defaults if the stats call fails: usage shown as zeros, storage None.
    cpu_avg_pct = 0.0
    cpu_max_pct = 0.0
    mem_avg_gib = 0.0
    mem_max_gib = 0.0
    storage_total_bytes = None
    storage_used_bytes = None

    stats_ok = False
    stats_path = (
        "/api/clustermgmt/v4.0/stats/clusters/{cid}/hosts/{hid}".format(
            cid=cluster_ext_id, hid=host_ext_id
        )
    )
    # clustermgmt stats reject a 'stats/'-prefixed $select (CLU-10007); omitting
    # it returns the full metric set, which is what we parse.
    params = dict(window.get("params") or {})
    try:
        payload = client.get_json(stats_path, params=params)

        cpu_samples = extract_samples(
            payload, metric_names.HOST_CPU_USAGE_PPM
        )
        cpu_avg_pct = units.ppm_to_pct(units.aggregate_avg(cpu_samples))
        cpu_max_pct = units.ppm_to_pct(units.aggregate_max(cpu_samples))

        mem_samples = extract_samples(
            payload, metric_names.HOST_MEM_USAGE_PPM
        )
        mem_avg_ppm = units.aggregate_avg(mem_samples)
        mem_max_ppm = units.aggregate_max(mem_samples)
        mem_avg_gib = units.mem_used_gib(mem_capacity_gib, mem_avg_ppm)
        mem_max_gib = units.mem_used_gib(mem_capacity_gib, mem_max_ppm)

        # Per-host storage is optional; show None if the version omits it.
        storage_total_bytes = latest_sample(
            payload, metric_names.HOST_STORAGE_CAPACITY_BYTES
        )
        storage_used_bytes = latest_sample(
            payload, metric_names.HOST_STORAGE_USAGE_BYTES
        )
        stats_ok = True
    except Exception as exc:
        LOG.warning(
            "Host %s (%s): stats collection failed (%s); row degraded",
            name,
            host_ext_id,
            exc,
        )

    return Host(
        name=name,
        cluster_name=cluster_name,
        ip=ip,
        cpu_capacity_hz=cpu_capacity_hz,
        cpu_avg_pct=cpu_avg_pct,
        cpu_max_pct=cpu_max_pct,
        mem_capacity_gib=mem_capacity_gib,
        mem_avg_gib=mem_avg_gib,
        mem_max_gib=mem_max_gib,
        storage_total_bytes=storage_total_bytes,
        storage_used_bytes=storage_used_bytes,
        stats_ok=stats_ok,
    )
