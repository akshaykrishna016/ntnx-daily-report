"""Cluster inventory and stats collection, including PC-cluster exclusion.

The cluster inventory returned by Prism Central includes the Prism Central
"self" cluster. Calling the stats API on it returns HTTP 400 (CLU-10008), which
crashed the first implementation attempt. This module filters that entity out
immediately after inventory (design document, section 5.3) by inspecting the
cluster function / role list, so no stats, hosts or VMs are ever collected from
the PC entity.

Cluster CPU and memory *capacity* are derived by summing the member hosts
(``build_cluster`` takes the already-collected hosts), which matches the
"derive from hosts" guidance in the spec. Usage percentages and storage come
from the cluster stats endpoint, with the whole stats call guarded so one
cluster's stats failure degrades that cluster rather than aborting the run.
"""

import logging

import units
from collector import metric_names
from collector.model import Cluster
from collector.parsing import extract_samples, latest_sample

LOG = logging.getLogger("ntnx.clusters")


def collect_cluster_inventory(client):
    """Fetch the cluster inventory and return only the AOS clusters.

    The Prism Central self-cluster is excluded here: any cluster whose function
    list contains ``PRISM_CENTRAL`` is dropped, and only clusters whose list
    contains ``AOS`` are kept. If the expected field is missing, one full
    cluster config object is DEBUG-logged so the operator can match on whatever
    role field the live PC actually exposes.

    Args:
        client: A PrismClient or MockClient.

    Returns:
        A list of raw AOS cluster dicts (inventory form, not yet ``Cluster``).
    """
    raw_clusters = client.paginate_v4(
        "/api/clustermgmt/v4.0/config/clusters"
    )
    LOG.info("Cluster inventory returned %d entities", len(raw_clusters))

    aos_clusters = []
    for raw in raw_clusters:
        functions = _cluster_functions(raw)
        if functions is None:
            LOG.debug(
                "Cluster %s has no recognizable function field; full config: %s",
                raw.get("name"),
                raw,
            )
        if metric_names.CLUSTER_FUNCTION_PRISM_CENTRAL in (functions or []):
            LOG.info(
                "Excluding Prism Central self-cluster '%s' from collection",
                raw.get("name"),
            )
            continue
        if metric_names.CLUSTER_FUNCTION_AOS in (functions or []):
            aos_clusters.append(raw)
        else:
            # Unknown function set: keep it but warn, so we neither silently
            # drop a real cluster nor assume it is the PC entity.
            LOG.warning(
                "Cluster '%s' has functions %s (no AOS marker); including it",
                raw.get("name"),
                functions,
            )
            aos_clusters.append(raw)

    LOG.info("Kept %d AOS cluster(s) after PC exclusion", len(aos_clusters))
    return aos_clusters


def _cluster_functions(raw):
    """Return the cluster function list from a raw cluster config, or None.

    Tries the documented ``config.clusterFunction`` location first, then a few
    fallbacks seen across AOS versions.
    """
    config = raw.get("config") or {}
    if isinstance(config, dict):
        functions = config.get(metric_names.CLUSTER_FUNCTION_FIELD)
        if functions is not None:
            return functions
    # Fallbacks: some versions expose the field at the top level.
    if raw.get("clusterFunction") is not None:
        return raw.get("clusterFunction")
    if raw.get("clusterFunctions") is not None:
        return raw.get("clusterFunctions")
    return None


def build_cluster(client, raw, hosts, window):
    """Build one ``Cluster`` from inventory, member hosts and cluster stats.

    Args:
        client: A PrismClient or MockClient.
        raw: The raw cluster inventory dict (already confirmed AOS).
        hosts: The list of ``Host`` records already collected for this cluster.
        window: Dict with stats query ``params``.

    Returns:
        A ``Cluster`` record. If the stats call fails the usage figures are
        zeros and storage is zero, and a warning is logged (the report still
        goes out).
    """
    cluster_ext_id = raw.get("extId")
    name = raw.get("name") or cluster_ext_id
    node_count = len(hosts)

    # Capacity is derived from the member hosts.
    cpu_capacity_hz = sum(host.cpu_capacity_hz for host in hosts)
    mem_capacity_gib = sum(host.mem_capacity_gib for host in hosts)

    cpu_avg_pct = 0.0
    cpu_max_pct = 0.0
    mem_avg_gib = 0.0
    mem_max_gib = 0.0
    storage_total_bytes = 0.0
    storage_used_bytes = 0.0

    stats_path = "/api/clustermgmt/v4.0/stats/clusters/{cid}".format(
        cid=cluster_ext_id
    )
    stats_ok = False
    # clustermgmt stats reject a 'stats/'-prefixed $select (CLU-10007); omitting
    # it returns the full metric set, which is what we parse.
    params = dict(window.get("params") or {})
    try:
        payload = client.get_json(stats_path, params=params)

        cpu_samples = extract_samples(
            payload, metric_names.CLUSTER_CPU_USAGE_PPM
        )
        cpu_avg_pct = units.ppm_to_pct(units.aggregate_avg(cpu_samples))
        cpu_max_pct = units.ppm_to_pct(units.aggregate_max(cpu_samples))

        mem_samples = extract_samples(
            payload, metric_names.CLUSTER_MEM_USAGE_PPM
        )
        mem_avg_gib = units.mem_used_gib(
            mem_capacity_gib, units.aggregate_avg(mem_samples)
        )
        mem_max_gib = units.mem_used_gib(
            mem_capacity_gib, units.aggregate_max(mem_samples)
        )

        storage_total_bytes = (
            latest_sample(payload, metric_names.CLUSTER_STORAGE_CAPACITY_BYTES)
            or 0.0
        )
        storage_used_bytes = (
            latest_sample(payload, metric_names.CLUSTER_STORAGE_USAGE_BYTES)
            or 0.0
        )
        stats_ok = True
    except Exception as exc:
        LOG.warning(
            "Cluster %s (%s): stats collection failed (%s); figures degraded",
            name,
            cluster_ext_id,
            exc,
        )

    return Cluster(
        name=name,
        ext_id=cluster_ext_id,
        node_count=node_count,
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
