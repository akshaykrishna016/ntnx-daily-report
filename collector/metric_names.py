"""Central mapping of Prism Central API metric field names.

The design document (section 5.3) is explicit that exact stats field names vary
by PC / AOS version. Rather than scatter these strings across every collector,
they live here in one place. When the probe script (see README, "First live
run") reports different field names on the target Prism Central, edit the
constants here and nowhere else.

None of these are executed as code paths at import time — they are plain string
constants describing the JSON keys the collectors look for.
"""

# ---- Cluster stats (GET /api/clustermgmt/v4.0/stats/clusters/{extId}) -------
# CPU usage in parts-per-million of total hypervisor CPU.
CLUSTER_CPU_USAGE_PPM = "hypervisorCpuUsagePpm"
# Aggregate hypervisor memory usage in parts-per-million.
CLUSTER_MEM_USAGE_PPM = "aggregateHypervisorMemoryUsagePpm"
# Storage capacity / usage are point-in-time byte counters.
CLUSTER_STORAGE_CAPACITY_BYTES = "storageCapacityBytes"
CLUSTER_STORAGE_USAGE_BYTES = "storageUsageBytes"

# ---- Host stats (GET .../stats/clusters/{cid}/hosts/{hid}) ------------------
HOST_CPU_USAGE_PPM = "hypervisorCpuUsagePpm"
HOST_MEM_USAGE_PPM = "hypervisorMemoryUsagePpm"
HOST_STORAGE_CAPACITY_BYTES = "storageCapacityBytes"
HOST_STORAGE_USAGE_BYTES = "storageUsageBytes"

# ---- VM stats (GET /api/vmm/v4.0/ahv/stats/vms/{extId}) ---------------------
VM_CPU_USAGE_PPM = "hypervisorCpuUsagePpm"
# The AHV VM stats schema exposes both a guest-reported memory figure
# ("memoryUsagePpm", requires NGT) and a hypervisor-observed one
# ("hypervisorMemoryUsagePpm", always available). The collector prefers the
# guest figure and falls back to the hypervisor figure when guest is absent.
VM_MEM_USAGE_PPM = "memoryUsagePpm"
VM_MEM_USAGE_PPM_ALT = "hypervisorMemoryUsagePpm"

# ---- $select projections (REQUIRED by the v4 stats endpoints) ----------------
# The v4 stats endpoints reject a request whose $select is empty (VMM-30102 /
# CLU-* invalid-argument). $select attributes MUST be prefixed with 'stats/'
# and comma-separated (confirmed in the OpenAPI specs). One place to edit if a
# metric name changes on a given AOS version.


def _select(*attributes):
    """Build a comma-separated, ``stats/``-prefixed $select value.

    Args:
        *attributes: Bare metric attribute names (no prefix).

    Returns:
        A string such as ``"stats/hypervisorCpuUsagePpm,stats/memoryUsagePpm"``.
    """
    return ",".join("stats/" + attribute for attribute in attributes)


CLUSTER_STATS_SELECT = _select(
    CLUSTER_CPU_USAGE_PPM,
    CLUSTER_MEM_USAGE_PPM,
    CLUSTER_STORAGE_CAPACITY_BYTES,
    CLUSTER_STORAGE_USAGE_BYTES,
)
HOST_STATS_SELECT = _select(
    HOST_CPU_USAGE_PPM,
    HOST_MEM_USAGE_PPM,
    HOST_STORAGE_CAPACITY_BYTES,
    HOST_STORAGE_USAGE_BYTES,
)
VM_STATS_SELECT = _select(
    VM_CPU_USAGE_PPM,
    VM_MEM_USAGE_PPM,
    VM_MEM_USAGE_PPM_ALT,
)

# Efficiency status strings that mean "no usable value" (insufficient baseline,
# policy-excluded, or recently created). Matched case-insensitively.
EFFICIENCY_NULL_TOKENS = {"na", "n/a", "measurementdisabled", "none", ""}

# ---- Cluster config field that identifies the Prism Central self-cluster ----
# The inventory includes the PC cluster itself; it must be excluded before any
# stats call (a stats call on it returns HTTP 400 CLU-10008). We match on the
# cluster function / role list.
CLUSTER_FUNCTION_FIELD = "clusterFunction"
CLUSTER_FUNCTION_PRISM_CENTRAL = "PRISM_CENTRAL"
CLUSTER_FUNCTION_AOS = "AOS"

# ---- v3 groups API attribute for VM efficiency ------------------------------
EFFICIENCY_ENTITY_TYPE = "mh_vm"
EFFICIENCY_ATTR_VM_NAME = "vm_name"
EFFICIENCY_ATTR_STATUS = "capacity.vm_efficiency_status"

# ---- v3 groups API attribute for storage runway -----------------------------
RUNWAY_ENTITY_TYPE = "cluster"
RUNWAY_ATTR = "capacity.runway"
