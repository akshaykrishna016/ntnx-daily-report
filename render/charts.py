"""Matplotlib chart generation, returned as base64 PNG data URIs.

All charts use the Agg (headless) backend so they render on a server with no
display. Each function returns a ``data:image/png;base64,...`` string ready to
drop straight into an ``<img src>`` in the self-contained report — there are no
CID references and no files left on disk (design document, sections 6 and 7a).

Charts rendered here (design document, section 6):
  * three capacity donuts (CPU / memory / storage), single consumed arc,
    colored by RAG band, percentage in the center;
  * Top-N VMs by CPU, horizontal bars colored by RAG;
  * Top-N VMs by memory, horizontal bars in deep purple.

Numbers baked into the charts are kept minimal (only the donut center %), since
the surrounding HTML text stays crisp and searchable.
"""

import base64
import io
import logging

import matplotlib

# Select the non-interactive backend before importing pyplot.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (must follow backend selection)

LOG = logging.getLogger("ntnx.charts")

# Brand palette (design document, section 6.1).
NX_PURPLE = "#7855FA"
NX_DEEP = "#4B00AA"
TRACK = "#F0F0F3"
RAG_GREEN = "#2E9E4F"
RAG_AMBER = "#E8A13D"
RAG_RED = "#D0342C"

# Charts render at 2x the display slot for retina sharpness.
_DONUT_PX = 280
_DPI = 100


def _rag_color(pct, amber_threshold, red_threshold):
    """Return the RAG hex color for a percentage (used by the donuts)."""
    if pct < amber_threshold:
        return RAG_GREEN
    if pct <= red_threshold:
        return RAG_AMBER
    return RAG_RED


def _cpu_bar_color(pct, amber_threshold, red_threshold):
    """Return the Top-Talker CPU bar color for a percentage.

    Per the design document (section 6): red above the red threshold, amber in
    the amber band, and brand purple (not RAG green) below the amber threshold.
    This matches the approved mockup, where sub-70% bars are purple.
    """
    if pct > red_threshold:
        return RAG_RED
    if pct >= amber_threshold:
        return RAG_AMBER
    return NX_PURPLE


def _fig_to_data_uri(fig):
    """Serialize a matplotlib figure to a base64 PNG data URI and close it."""
    buffer = io.BytesIO()
    fig.savefig(
        buffer,
        format="png",
        dpi=_DPI,
        transparent=True,
        bbox_inches="tight",
        pad_inches=0.02,
    )
    plt.close(fig)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return "data:image/png;base64," + encoded


def donut(consumed_pct, amber_threshold, red_threshold):
    """Render one capacity donut and return it as a PNG data URI.

    Args:
        consumed_pct: The consumed percentage (0-100).
        amber_threshold: RAG amber threshold.
        red_threshold: RAG red threshold.

    Returns:
        A base64 PNG data URI string.
    """
    consumed = max(0.0, min(100.0, consumed_pct))
    color = _rag_color(consumed, amber_threshold, red_threshold)

    inches = _DONUT_PX / _DPI
    fig, axis = plt.subplots(figsize=(inches, inches))

    # Draw the full track first, then the consumed arc on top.
    axis.pie(
        [1],
        colors=[TRACK],
        radius=1.0,
        startangle=90,
        counterclock=False,
        wedgeprops={"width": 0.30, "edgecolor": "none"},
    )
    axis.pie(
        [consumed, 100 - consumed],
        colors=[color, "none"],
        radius=1.0,
        startangle=90,
        counterclock=False,
        wedgeprops={"width": 0.30, "edgecolor": "none"},
    )

    axis.text(
        0,
        0.05,
        "{:d}%".format(int(round(consumed))),
        ha="center",
        va="center",
        fontsize=26,
        fontweight="bold",
        color="#131313",
    )
    axis.text(
        0,
        -0.22,
        "consumed",
        ha="center",
        va="center",
        fontsize=11,
        color="#6B7480",
    )
    axis.set(aspect="equal")
    axis.axis("off")
    return _fig_to_data_uri(fig)


def top_vms_cpu_bar(rows, amber_threshold, red_threshold):
    """Render the Top-N VMs by CPU horizontal bar chart.

    Args:
        rows: List of ``(vm_name, cpu_pct)`` tuples, already sorted descending.
        amber_threshold: RAG amber threshold (bar color).
        red_threshold: RAG red threshold (bar color).

    Returns:
        A base64 PNG data URI string.
    """
    labels = [name for (name, _value) in rows]
    values = [value for (_name, value) in rows]
    colors = [
        _cpu_bar_color(value, amber_threshold, red_threshold)
        for value in values
    ]
    value_labels = ["{:d}%".format(int(round(v))) for v in values]
    return _horizontal_bars(labels, values, colors, value_labels, x_max=100)


def top_vms_mem_bar(rows):
    """Render the Top-N VMs by memory horizontal bar chart (deep purple).

    Args:
        rows: List of ``(vm_name, mem_gib)`` tuples, already sorted descending.

    Returns:
        A base64 PNG data URI string.
    """
    labels = [name for (name, _value) in rows]
    values = [value for (_name, value) in rows]
    colors = [NX_DEEP for _ in values]
    value_labels = ["{:d} GiB".format(int(round(v))) for v in values]
    x_max = max(values) * 1.18 if values else 1
    return _horizontal_bars(labels, values, colors, value_labels, x_max=x_max)


def _horizontal_bars(labels, values, colors, value_labels, x_max):
    """Shared horizontal-bar renderer used by both top-talker charts."""
    row_count = max(1, len(labels))
    fig_height = 0.34 * row_count + 0.2
    fig, axis = plt.subplots(figsize=(4.6, fig_height), dpi=_DPI)

    y_positions = list(range(row_count))
    # Plot so the highest value sits at the top.
    axis.barh(
        y_positions,
        list(reversed(values)),
        color=list(reversed(colors)),
        height=0.62,
    )
    axis.set_yticks(y_positions)
    axis.set_yticklabels(
        list(reversed(labels)), fontsize=8.5, color="#5D5D5D"
    )
    axis.set_xlim(0, x_max)
    axis.set_xticks([])
    for spine in ("top", "right", "bottom", "left"):
        axis.spines[spine].set_visible(False)

    # Value labels at the end of each bar.
    for y_pos, value, text in zip(
        y_positions, list(reversed(values)), list(reversed(value_labels))
    ):
        axis.text(
            value + x_max * 0.01,
            y_pos,
            text,
            va="center",
            ha="left",
            fontsize=8.5,
            fontweight="bold",
            color="#131313",
        )
    axis.margins(y=0.02)
    return _fig_to_data_uri(fig)
