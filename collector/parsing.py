"""Helpers that turn raw Prism Central JSON into plain Python values.

Two response families need parsing:

1. v4 stats responses, shaped as ``{"data": [ {metric: [{timestamp, value}]} ]}``.
   ``extract_samples`` pulls the numeric value series for one metric so the
   aggregation functions in ``units.py`` can compute average and peak.

2. v3 groups responses, whose entities carry a ``data`` list of
   ``{name, values:[{values:[<value>]}]}`` attribute records.
   ``groups_entities`` and ``entity_attr`` read those.

Keeping this parsing in one place means the collectors stay readable and the
exact JSON nesting is only spelled out once.
"""


def extract_samples(stats_payload, metric_name):
    """Extract the numeric value series for one metric from a v4 stats payload.

    The v4 stats response shape (confirmed in the OpenAPI specs) is a list of
    per-timestamp sample objects, each carrying a ``timestamp`` plus the
    selected metrics as scalar values::

        {"data": [
            {"timestamp": "...", "hypervisorCpuUsagePpm": 94, "memoryUsagePpm": 61},
            {"timestamp": "...", "hypervisorCpuUsagePpm": 88, "memoryUsagePpm": 60},
            ...
        ]}

    So the series for one metric is that metric's scalar value taken from each
    sample object. For robustness this also accepts two alternative shapes seen
    across versions: ``data`` as a single object (not a list), and a metric
    whose value is itself a list of ``{"value": n}`` points.

    Args:
        stats_payload: The dict returned by a stats GET.
        metric_name: The metric key to read (bare name, no ``stats/`` prefix).

    Returns:
        A list of numeric values (floats/ints). Empty when the metric is
        absent or the payload is empty — the caller decides how to degrade.
    """
    data = stats_payload.get("data")
    if data is None:
        return []

    # VMM shape (VM stats): metrics are scalars inside ``data.stats[]``.
    if isinstance(data, dict) and isinstance(data.get("stats"), list):
        values = []
        for tuple_entry in data["stats"]:
            if isinstance(tuple_entry, dict):
                value = tuple_entry.get(metric_name)
                if value is not None:
                    values.append(value)
        return values

    # clustermgmt shape (cluster / host stats): the metric is its own array of
    # ``{timestamp, value}`` points directly under ``data``.
    if isinstance(data, dict):
        return _pair_values(data.get(metric_name))

    # Legacy list shape: ``data`` is a list of sample objects.
    if isinstance(data, list):
        values = []
        for sample in data:
            if not isinstance(sample, dict):
                continue
            raw = sample.get(metric_name)
            if raw is None:
                continue
            if isinstance(raw, list):
                values.extend(_pair_values(raw))
            else:
                values.append(raw)
        return values

    return []


def _pair_values(array):
    """Return the numeric values from a list of ``{timestamp, value}`` points.

    Accepts dict points (``{"value": n}``) or bare numbers.
    """
    if not isinstance(array, list):
        return []
    values = []
    for point in array:
        if isinstance(point, dict):
            if point.get("value") is not None:
                values.append(point["value"])
        elif point is not None:
            values.append(point)
    return values


def latest_sample(stats_payload, metric_name):
    """Return the most recent value for a metric (used for storage counters).

    For the clustermgmt shape the metric is an array of ``{timestamp, value}``
    points that can arrive newest-first, so the value with the greatest
    timestamp is chosen. Falls back to the last value in the series when there
    are no timestamps (e.g. the VMM shape or the fixtures).

    Args:
        stats_payload: A v4 stats payload.
        metric_name: The metric key to read.

    Returns:
        The most recent numeric value, or ``None`` when absent.
    """
    data = stats_payload.get("data")
    if isinstance(data, dict):
        array = data.get(metric_name)
        if (
            isinstance(array, list)
            and array
            and isinstance(array[0], dict)
            and "timestamp" in array[0]
        ):
            newest = max(array, key=lambda point: point.get("timestamp") or "")
            return newest.get("value")

    values = extract_samples(stats_payload, metric_name)
    if not values:
        return None
    return values[-1]


def groups_entities(groups_payload):
    """Return the flat list of entity_result dicts from a groups response.

    Args:
        groups_payload: The dict returned by a POST to /api/nutanix/v3/groups.

    Returns:
        A list of entity dicts (each with an ``entity_id`` and ``data`` list).
    """
    entities = []
    for group in groups_payload.get("group_results") or []:
        entities.extend(group.get("entity_results") or [])
    return entities


def entity_attr(entity, attr_name):
    """Read one attribute value from a groups entity.

    The groups shape nests values as
    ``data: [ {name, values: [ {values: [<value>]} ]} ]``.

    Args:
        entity: One entity_result dict.
        attr_name: The attribute (column) name to read.

    Returns:
        The attribute's first value as a string, or ``None`` when absent.
    """
    for record in entity.get("data") or []:
        if record.get("name") == attr_name:
            values = record.get("values") or []
            if not values:
                return None
            inner = values[0].get("values") or []
            if not inner:
                return None
            return inner[0]
    return None
