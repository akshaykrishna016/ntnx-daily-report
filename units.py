"""Unit-conversion and normalization helpers for the Nutanix daily report.

Every function here is a pure function: it takes numbers (or simple values) in,
and returns numbers or strings out, with no side effects and no I/O. Keeping all
of the math in one small module means it can be unit-tested exhaustively (see
tests/test_transforms.py) and reused identically by every collector and by the
renderer, so display numbers never drift between the tables and the charts.

Unit rules come straight from the design document, section 5.4:

- ppm -> %        : divide by 10,000.
- bytes -> GiB    : divide by 1024**3 (binary gibibytes).
- bytes -> TiB    : divide by 1024**4 (binary tebibytes).
- Hz -> GHz       : divide by 1e9.
- Memory used GiB : capacity_gib * usage_ppm / 1_000_000.

This module deliberately uses only the Python standard library.
"""

# Number of parts-per-million in one whole (100%).
PPM_PER_WHOLE = 1_000_000

# Number of parts-per-million that equal one percent (1% = 10,000 ppm).
PPM_PER_PERCENT = 10_000

# Binary size bases.
BYTES_PER_GIB = 1024 ** 3
BYTES_PER_TIB = 1024 ** 4

# Hertz per gigahertz.
HZ_PER_GHZ = 1_000_000_000


def ppm_to_pct(ppm):
    """Convert a parts-per-million usage value to a percentage.

    Nutanix v4 stats report CPU and memory usage in ppm, where 1,000,000 ppm
    means 100%. Dividing by 10,000 yields a percentage.

    Args:
        ppm: Usage in parts-per-million (int or float). ``None`` is treated as 0.

    Returns:
        The usage as a float percentage (not yet rounded for display).
    """
    if ppm is None:
        return 0.0
    return float(ppm) / PPM_PER_PERCENT


def pct_round(pct):
    """Round a percentage to a whole number for display.

    Args:
        pct: A percentage value (float).

    Returns:
        The percentage as an int, rounded half-to-even by Python's ``round``.
    """
    return int(round(pct))


def bytes_to_gib(num_bytes):
    """Convert a byte count to binary gibibytes (GiB).

    Args:
        num_bytes: Size in bytes. ``None`` is treated as 0.

    Returns:
        The size in GiB as a float.
    """
    if num_bytes is None:
        return 0.0
    return float(num_bytes) / BYTES_PER_GIB


def bytes_to_tib(num_bytes):
    """Convert a byte count to binary tebibytes (TiB).

    Args:
        num_bytes: Size in bytes. ``None`` is treated as 0.

    Returns:
        The size in TiB as a float.
    """
    if num_bytes is None:
        return 0.0
    return float(num_bytes) / BYTES_PER_TIB


def hz_to_ghz(hz):
    """Convert a frequency in hertz to gigahertz (GHz).

    Args:
        hz: Frequency in hertz. ``None`` is treated as 0.

    Returns:
        The frequency in GHz as a float.
    """
    if hz is None:
        return 0.0
    return float(hz) / HZ_PER_GHZ


def mem_used_gib(capacity_gib, usage_ppm):
    """Compute memory used in GiB from a capacity and a ppm usage figure.

    Args:
        capacity_gib: Total memory capacity in GiB.
        usage_ppm: Memory usage in parts-per-million.

    Returns:
        Memory used in GiB as a float.
    """
    if capacity_gib is None or usage_ppm is None:
        return 0.0
    return float(capacity_gib) * float(usage_ppm) / PPM_PER_WHOLE


def rag_class(pct, amber_threshold, red_threshold):
    """Classify a percentage into a Red/Amber/Green status band.

    The design document (section 6.1) defines the thresholds as:
    green < amber_threshold, amber between the two (inclusive of amber, up to
    and including red), red > red_threshold. Boundary behaviour matters and is
    unit-tested: at exactly the amber threshold the value is amber; at exactly
    the red threshold the value is still amber; only values strictly greater
    than the red threshold are red.

    Args:
        pct: The percentage to classify.
        amber_threshold: Lower bound where amber begins (e.g. 70).
        red_threshold: Upper bound where red begins above it (e.g. 85).

    Returns:
        One of the strings ``"green"``, ``"amber"`` or ``"red"``.
    """
    if pct < amber_threshold:
        return "green"
    if pct <= red_threshold:
        return "amber"
    return "red"


def aggregate_avg(samples):
    """Return the arithmetic mean of a list of numeric samples.

    Used to turn a 5-minute sample series from the stats API into the "average
    over the window" figure shown in the "Usage %" columns.

    Args:
        samples: A list of numbers. Empty or ``None`` yields 0.0.

    Returns:
        The mean as a float, or 0.0 when there are no samples.
    """
    if not samples:
        return 0.0
    return float(sum(samples)) / len(samples)


def aggregate_max(samples):
    """Return the peak (maximum) of a list of numeric samples.

    Used to turn a 5-minute sample series into the "MAX" columns.

    Args:
        samples: A list of numbers. Empty or ``None`` yields 0.0.

    Returns:
        The maximum as a float, or 0.0 when there are no samples.
    """
    if not samples:
        return 0.0
    return float(max(samples))


# Canonical efficiency-status labels the report understands. The groups API may
# return these in different case or spacing, so normalization maps onto these.
EFFICIENCY_CANONICAL = [
    "Bully",
    "Constrained",
    "Overprovisioned",
    "Inactive",
    "Good",
]

# Lookup from a lower-cased, stripped API string to its canonical label. A few
# known API variants (with underscores or extra words) are mapped explicitly.
_EFFICIENCY_LOOKUP = {
    "bully": "Bully",
    "constrained": "Constrained",
    "overprovisioned": "Overprovisioned",
    "over_provisioned": "Overprovisioned",
    "over-provisioned": "Overprovisioned",
    "inactive": "Inactive",
    "good": "Good",
    "optimal": "Good",
}

# Values that mean "no usable efficiency data" (insufficient X-FIT baseline,
# policy-excluded, or recently created). These are ignored silently rather than
# logged as unrecognized.
_EFFICIENCY_NULL_TOKENS = {"na", "n/a", "none", "null", "measurementdisabled",
                          ""}


def normalize_efficiency_statuses(raw_status):
    """Normalize an efficiency-status value into a list of canonical labels.

    The live Prism Central was observed to return more than the single scalar
    the schema implies: some VMs come back with two comma-joined statuses (e.g.
    ``"Constrained,Overprovisioned"``) and some with ``"NA"``. This splits on
    commas, normalizes each part case-insensitively, drops null tokens silently,
    and returns the recognized canonical labels.

    Args:
        raw_status: The raw string from ``capacity.vm_efficiency_status``.

    Returns:
        A list of canonical labels (possibly empty). Parts that are neither a
        known status nor a null token are omitted; the caller may log them via
        ``unrecognized_efficiency_parts``.
    """
    if not raw_status:
        return []
    labels = []
    for part in str(raw_status).split(","):
        key = part.strip().lower()
        if key in _EFFICIENCY_NULL_TOKENS:
            continue
        canonical = _EFFICIENCY_LOOKUP.get(key)
        if canonical and canonical not in labels:
            labels.append(canonical)
    return labels


def unrecognized_efficiency_parts(raw_status):
    """Return the parts of an efficiency value that are neither known nor null.

    Used only for logging so genuinely unexpected values surface, while ``NA``
    and friends stay quiet.

    Args:
        raw_status: The raw efficiency string.

    Returns:
        A list of the raw (original-case) parts that were not recognized.
    """
    if not raw_status:
        return []
    unknown = []
    for part in str(raw_status).split(","):
        stripped = part.strip()
        key = stripped.lower()
        if key in _EFFICIENCY_NULL_TOKENS:
            continue
        if key not in _EFFICIENCY_LOOKUP:
            unknown.append(stripped)
    return unknown


def normalize_efficiency_status(raw_status):
    """Normalize an efficiency-status string to a single canonical label.

    Backward-compatible helper that returns the first recognized label, or
    ``None`` when the value is empty, a null token, or unrecognized.

    Args:
        raw_status: The raw string from ``capacity.vm_efficiency_status``.

    Returns:
        A canonical label from ``EFFICIENCY_CANONICAL``, or ``None``.
    """
    labels = normalize_efficiency_statuses(raw_status)
    return labels[0] if labels else None


def fmt_int(value):
    """Format a number as an integer with thousands separators.

    Args:
        value: A number.

    Returns:
        A string such as ``"4,977"``.
    """
    return "{:,}".format(int(round(value)))


def fmt_tib(num_bytes):
    """Format a byte count as a TiB string with one decimal place.

    Args:
        num_bytes: Size in bytes.

    Returns:
        A string such as ``"87.4 TiB"``.
    """
    return "{:.1f} TiB".format(bytes_to_tib(num_bytes))


def fmt_ghz(hz):
    """Format a hertz value as a GHz string with one decimal place.

    Args:
        hz: Frequency in hertz.

    Returns:
        A string such as ``"364.8"`` (no unit suffix; the column header carries
        the unit).
    """
    return "{:.1f}".format(hz_to_ghz(hz))


def fmt_storage(num_bytes):
    """Format a storage size, choosing GiB for small volumes and TiB for large.

    VM disks are often well under a tebibyte, so showing "0.2 TiB" reads poorly.
    Below 1 TiB the value is shown in whole GiB; at or above 1 TiB it is shown
    in TiB with one decimal, matching the mockup (e.g. "500 GiB", "2.0 TiB").

    Args:
        num_bytes: Size in bytes.

    Returns:
        A formatted storage string.
    """
    if num_bytes is None:
        return "—"  # em dash
    if num_bytes < BYTES_PER_TIB:
        return "{:,} GiB".format(int(round(bytes_to_gib(num_bytes))))
    return "{:.1f} TiB".format(bytes_to_tib(num_bytes))
