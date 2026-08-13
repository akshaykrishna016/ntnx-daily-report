"""Internal data model for the Nutanix daily report.

The collectors normalize the varied REST API responses into the plain
dataclasses defined here (design document, section 5.4). Everything downstream
of collection — charts, HTML template, CSV writer — reads these objects and
nothing else, so the rest of the codebase never has to know the exact shape of
a Prism Central API payload.

All sizes are stored in their natural base unit (bytes, Hz) and converted for
display by ``units.py`` at render time. Percentages are stored already
converted to a 0-100 float.

Compatible with Python 3.9 (uses ``typing.Optional`` and ``dataclasses``).
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Cluster:
    """One AOS cluster's inventory plus its aggregated window stats."""

    name: str
    ext_id: str
    node_count: int
    cpu_capacity_hz: float
    cpu_avg_pct: float
    cpu_max_pct: float
    mem_capacity_gib: float
    mem_avg_gib: float
    mem_max_gib: float
    storage_total_bytes: float
    storage_used_bytes: float
    stats_ok: bool = True

    @property
    def mem_max_gib_value(self):
        """Peak memory in GiB (alias kept for template readability)."""
        return self.mem_max_gib

    @property
    def storage_free_bytes(self):
        """Free storage in bytes (total minus used)."""
        return self.storage_total_bytes - self.storage_used_bytes


@dataclass
class Host:
    """One hypervisor host's inventory plus its aggregated window stats."""

    name: str
    cluster_name: str
    ip: str
    cpu_capacity_hz: float
    cpu_avg_pct: float
    cpu_max_pct: float
    mem_capacity_gib: float
    mem_avg_gib: float
    mem_max_gib: float
    storage_total_bytes: Optional[float]
    storage_used_bytes: Optional[float]
    stats_ok: bool = True

    @property
    def storage_free_bytes(self):
        """Free storage in bytes, or ``None`` when host storage is unavailable."""
        if self.storage_total_bytes is None or self.storage_used_bytes is None:
            return None
        return self.storage_total_bytes - self.storage_used_bytes


@dataclass
class VM:
    """One guest VM's inventory plus its aggregated window stats.

    ``guest_used_bytes`` / ``guest_free_bytes`` are ``None`` when Nutanix Guest
    Tools (NGT) is not installed, in which case the report shows an em dash and
    still reports the hypervisor-allocated ``storage_total_bytes``.
    ``efficiency_status`` is ``None`` when the groups API returned no value.
    ``stats_ok`` is ``False`` when this VM's stats call failed and its usage
    figures could not be collected (they show as em dashes in the report).
    """

    name: str
    cluster_name: str
    vcpus: int
    cpu_avg_pct: float
    cpu_max_pct: float
    mem_capacity_gib: float
    mem_avg_gib: float
    mem_max_gib: float
    storage_total_bytes: float
    guest_used_bytes: Optional[int]
    guest_free_bytes: Optional[int]
    disk_count: int
    power_state: str
    efficiency_status: Optional[str] = None
    stats_ok: bool = True


@dataclass
class Summary:
    """Estate-wide roll-ups used by the KPI strip, gauges and email body."""

    cluster_count: int
    clusters_healthy: int
    host_count: int
    vm_total: int
    vm_on: int
    vm_off: int

    # Aggregate capacity for the three executive gauges.
    cpu_capacity_hz: float
    cpu_used_hz: float
    cpu_pct: float
    mem_capacity_gib: float
    mem_used_gib: float
    mem_pct: float
    storage_total_bytes: float
    storage_used_bytes: float
    storage_pct: float

    # Efficiency tile counts, keyed by canonical label.
    efficiency_counts: dict = field(default_factory=dict)
    efficiency_available: bool = True

    # Optional KPI values (may be None when their call failed / degraded).
    critical_alert_count: Optional[int] = None
    storage_runway_days: Optional[int] = None

    # Count of VMs whose stats could not be fully collected (footer note).
    vm_stats_failures: int = 0
