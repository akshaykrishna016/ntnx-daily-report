"""VM efficiency states and optional KPI values via the v3 groups API.

There is no v4 endpoint for VM efficiency, so this uses the same v3 groups call
the Prism Central UI makes (design document, section 5.3). It also gathers the
two optional KPI values that share the groups API: the critical-alert count and
the storage runway. Every call degrades gracefully — a failure here yields
"N/A" tiles or "n/a" KPI values and never blocks the report.
"""

import logging

import units
from collector import metric_names
from collector.parsing import groups_entities, entity_attr

LOG = logging.getLogger("ntnx.efficiency")

# The groups query body for VM efficiency states.
_EFFICIENCY_BODY = {
    "entity_type": metric_names.EFFICIENCY_ENTITY_TYPE,
    "group_member_count": 500,
    "group_member_offset": 0,
    "group_member_attributes": [
        {"attribute": metric_names.EFFICIENCY_ATTR_VM_NAME},
        {"attribute": metric_names.EFFICIENCY_ATTR_STATUS},
    ],
    "filter_criteria": (
        "(platform_type!=aws,platform_type==[no_val]);is_cvm==0;"
        "feature_name==VM_INEFFICIENCY_DETECTION"
    ),
}

# The groups query body for guest disk usage (NGT-sourced).
_GUEST_STORAGE_BODY = {
    "entity_type": metric_names.EFFICIENCY_ENTITY_TYPE,
    "group_member_count": 500,
    "group_member_offset": 0,
    "group_member_attributes": [
        {"attribute": "vm_name"},
        {"attribute": "guest.disk_capacity_bytes"},
        {"attribute": "guest.disk_usage_bytes"},
    ],
    "filter_criteria": "is_cvm==0",
}


def collect_efficiency_by_vm(client):
    """Return a mapping of VM name -> list of canonical efficiency statuses.

    A VM may carry more than one status on the live PC (the value can come back
    comma-joined, e.g. ``"Constrained,Overprovisioned"``); ``NA`` and other null
    tokens are ignored. Only genuinely unrecognized parts are logged.

    Args:
        client: A PrismClient or MockClient.

    Returns:
        A tuple ``(status_by_vm, available)``. ``status_by_vm`` maps VM name to
        a list of canonical status labels (VMs with no usable status are
        omitted). ``available`` is ``False`` when the groups call failed
        entirely, so the caller can show "N/A" tiles with the footnote.
    """
    status_by_vm = {}
    try:
        payload = client.post_json(
            "/api/nutanix/v3/groups", _EFFICIENCY_BODY
        )
    except Exception as exc:
        LOG.warning("Efficiency groups call failed (%s); tiles show N/A", exc)
        return status_by_vm, False

    for entity in groups_entities(payload):
        vm_name = entity_attr(entity, metric_names.EFFICIENCY_ATTR_VM_NAME)
        raw_status = entity_attr(entity, metric_names.EFFICIENCY_ATTR_STATUS)
        if not vm_name:
            continue
        labels = units.normalize_efficiency_statuses(raw_status)
        if labels:
            status_by_vm[vm_name] = labels
        unknown = units.unrecognized_efficiency_parts(raw_status)
        if unknown:
            LOG.warning(
                "Unrecognized efficiency status %s for VM %s",
                unknown,
                vm_name,
            )
    return status_by_vm, True


def collect_guest_storage_by_vm(client):
    """Return a mapping of VM name -> (used_bytes, free_bytes) from NGT data.

    VMs without NGT are simply absent from the result (the caller renders an em
    dash for them). A failure of the whole call returns an empty map — guest
    storage is a soft requirement and must never fail the report.

    Args:
        client: A PrismClient or MockClient.

    Returns:
        Dict mapping VM name to a ``(used_bytes, free_bytes)`` tuple.
    """
    result = {}
    try:
        payload = client.post_json(
            "/api/nutanix/v3/groups", _GUEST_STORAGE_BODY
        )
    except Exception as exc:
        LOG.warning(
            "Guest-storage groups call failed (%s); guest columns show '—'",
            exc,
        )
        return result

    for entity in groups_entities(payload):
        vm_name = entity_attr(entity, "vm_name")
        capacity = entity_attr(entity, "guest.disk_capacity_bytes")
        used = entity_attr(entity, "guest.disk_usage_bytes")
        if not vm_name or capacity is None or used is None:
            continue
        try:
            capacity_bytes = int(capacity)
            used_bytes = int(used)
        except (TypeError, ValueError):
            continue
        free_bytes = max(0, capacity_bytes - used_bytes)
        result[vm_name] = (used_bytes, free_bytes)
    return result


def collect_critical_alert_count(client):
    """Return the count of unresolved critical alerts in the window, or None.

    Args:
        client: A PrismClient or MockClient.

    Returns:
        An int count, or ``None`` when the call failed (KPI shows "n/a").
    """
    body = {
        "entity_type": "alert",
        "group_member_count": 500,
        "group_member_offset": 0,
        "group_member_attributes": [{"attribute": "title"}],
        "filter_criteria": "severity==kCritical;resolved==false",
    }
    try:
        payload = client.post_json("/api/nutanix/v3/groups", body)
    except Exception as exc:
        LOG.warning("Alerts groups call failed (%s); KPI shows n/a", exc)
        return None

    # Prefer the server-reported filtered count; fall back to entity count.
    count = payload.get("filtered_entity_count")
    if count is None:
        count = len(groups_entities(payload))
    return int(count)


def collect_storage_runway_days(client):
    """Return the worst-cluster storage runway in days, or None.

    Args:
        client: A PrismClient or MockClient.

    Returns:
        The minimum (worst) runway across clusters as an int, or ``None`` when
        the call failed or no value was present.
    """
    body = {
        "entity_type": metric_names.RUNWAY_ENTITY_TYPE,
        "group_member_count": 100,
        "group_member_offset": 0,
        "group_member_attributes": [
            {"attribute": "cluster_name"},
            {"attribute": metric_names.RUNWAY_ATTR},
        ],
    }
    try:
        payload = client.post_json("/api/nutanix/v3/groups", body)
    except Exception as exc:
        LOG.warning("Runway groups call failed (%s); KPI shows n/a", exc)
        return None

    runways = []
    for entity in groups_entities(payload):
        raw = entity_attr(entity, metric_names.RUNWAY_ATTR)
        if raw is None:
            continue
        try:
            runways.append(int(float(raw)))
        except (TypeError, ValueError):
            continue
    if not runways:
        return None
    return min(runways)
