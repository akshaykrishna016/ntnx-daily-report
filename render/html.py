"""HTML rendering: the self-contained attached report and the email body.

Two outputs are produced here:

1. ``render_report`` fills the jinja template ``templates/report.html.j2`` to
   produce the fully self-contained report attachment (design document, section
   7a): all charts and logos are base64 data URIs, all CSS is in one ``<style>``
   block, and there are zero external references.

2. ``build_email_body`` produces the compact, image-free, table-based summary
   used as the email body (section 7b), plus a matching plain-text version. It
   is built in Python rather than a template because every style must be inlined
   for Outlook and the structure is small.

Display formatting (percent rounding, RAG classes, size strings) happens here so
the template and the body stay purely presentational.
"""

import base64
import logging
import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

import units
from render.charts import (
    donut,
    top_vms_cpu_bar,
    top_vms_mem_bar,
)

LOG = logging.getLogger("ntnx.html")

# Map a RAG band name to the CSS class used in the report tables.
_RAG_CLASS = {"green": "rag-g", "amber": "rag-a", "red": "rag-r"}

# Map a RAG band to an inline background color for the email body table.
_RAG_BG = {"green": "#2E9E4F", "amber": "#E8A13D", "red": "#D0342C"}

EM_DASH = "—"

# Efficiency tile presentation (label -> background color + dark-text flag),
# in the mockup's order.
_TILE_STYLE = [
    ("Inactive", "#5D5D5D", False, "powered off / idle >30d"),
    ("Overprovisioned", "#0092B0", False, "reclaimable resources"),
    ("Constrained", "#FF9178", True, "need more resources"),
    ("Bully", "#391699", False, "starving neighbours"),
]


def embed_image_file(path):
    """Read an image file and return it as a base64 data URI, or None.

    Missing files return ``None`` so the caller can omit the ``<img>`` cleanly
    (design document, section 7a: logos embed only if present).

    Args:
        path: Path to a PNG (or other image) file.

    Returns:
        A ``data:image/png;base64,...`` string, or ``None`` if the file does
        not exist.
    """
    if not path or not os.path.isfile(path):
        return None
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return "data:image/png;base64," + encoded


def _rag_class(pct, thresholds):
    """Return the RAG CSS class for a percentage."""
    band = units.rag_class(pct, thresholds["amber"], thresholds["red"])
    return _RAG_CLASS[band]


def _entity_row(entity, thresholds):
    """Build the display dict for a cluster or host table row.

    When the entity's stats call failed (``stats_ok`` is False) the usage cells
    render as em dashes rather than a misleading green "0%", since zero usage
    would read as "healthy and idle" when it actually means "no data".
    """
    storage_total = entity.storage_total_bytes
    storage_free = entity.storage_free_bytes
    stats_ok = getattr(entity, "stats_ok", True)

    if stats_ok:
        cpu_pct = units.pct_round(entity.cpu_avg_pct)
        cpu_pct_rag = _rag_class(entity.cpu_avg_pct, thresholds)
        cpu_max = units.pct_round(entity.cpu_max_pct)
        cpu_max_rag = _rag_class(entity.cpu_max_pct, thresholds)
        mem_used = units.fmt_int(entity.mem_avg_gib)
        mem_max = units.fmt_int(entity.mem_max_gib)
        used = (
            units.fmt_tib(entity.storage_used_bytes)
            if entity.storage_used_bytes is not None
            else EM_DASH
        )
        free = units.fmt_tib(storage_free) if storage_free is not None \
            else EM_DASH
        storage = (
            units.fmt_tib(storage_total)
            if storage_total is not None
            else EM_DASH
        )
    else:
        cpu_pct = EM_DASH
        cpu_pct_rag = ""
        cpu_max = EM_DASH
        cpu_max_rag = ""
        mem_used = EM_DASH
        mem_max = EM_DASH
        used = EM_DASH
        free = EM_DASH
        storage = (
            units.fmt_tib(storage_total)
            if storage_total is not None
            else EM_DASH
        )

    return {
        "name": entity.name,
        "ip": getattr(entity, "ip", ""),
        "cpu_ghz": units.fmt_ghz(entity.cpu_capacity_hz),
        "cpu_pct": cpu_pct,
        "cpu_pct_rag": cpu_pct_rag,
        "cpu_max": cpu_max,
        "cpu_max_rag": cpu_max_rag,
        "mem": units.fmt_int(entity.mem_capacity_gib),
        "mem_used": mem_used,
        "mem_max": mem_max,
        "storage": storage,
        "used": used,
        "free": free,
    }


def _vm_row(vm, thresholds):
    """Build the display dict for a VM table row."""
    if vm.stats_ok:
        cpu_max = units.pct_round(vm.cpu_max_pct)
        cpu_max_rag = _rag_class(vm.cpu_max_pct, thresholds)
        mem_used = units.fmt_int(vm.mem_avg_gib)
        mem_max = units.fmt_int(vm.mem_max_gib)
    else:
        cpu_max = EM_DASH
        cpu_max_rag = ""
        mem_used = EM_DASH
        mem_max = EM_DASH

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
    return {
        "name": vm.name,
        "vcpu": vm.vcpus,
        "cpu_max": cpu_max,
        "cpu_max_rag": cpu_max_rag,
        "mem": units.fmt_int(vm.mem_capacity_gib),
        "mem_used": mem_used,
        "mem_max": mem_max,
        "storage": units.fmt_storage(vm.storage_total_bytes),
        "guest_used": guest_used,
        "guest_free": guest_free,
        "disks": vm.disk_count,
    }


def build_charts(summary, vms, config):
    """Render every chart image and return them keyed for the template.

    Args:
        summary: The ``Summary`` (for the three gauges).
        vms: All in-scope VMs (for the two top-talker charts).
        config: Parsed config (thresholds, top_n_vms).

    Returns:
        A dict with the gauge and bar-chart data URIs.
    """
    thresholds = config["report"]["thresholds"]
    top_n = config["report"].get("top_n_vms", 10)

    # Only VMs with valid stats can be ranked.
    ranked_by_cpu = sorted(
        (vm for vm in vms if vm.stats_ok),
        key=lambda vm: vm.cpu_avg_pct,
        reverse=True,
    )[:top_n]
    ranked_by_mem = sorted(
        (vm for vm in vms if vm.stats_ok),
        key=lambda vm: vm.mem_avg_gib,
        reverse=True,
    )[:top_n]

    cpu_rows = [(vm.name, vm.cpu_avg_pct) for vm in ranked_by_cpu]
    mem_rows = [(vm.name, vm.mem_avg_gib) for vm in ranked_by_mem]

    return {
        "cpu_gauge": donut(
            summary.cpu_pct, thresholds["amber"], thresholds["red"]
        ),
        "mem_gauge": donut(
            summary.mem_pct, thresholds["amber"], thresholds["red"]
        ),
        "storage_gauge": donut(
            summary.storage_pct, thresholds["amber"], thresholds["red"]
        ),
        "cpu_bar": top_vms_cpu_bar(
            cpu_rows, thresholds["amber"], thresholds["red"]
        ),
        "mem_bar": top_vms_mem_bar(mem_rows),
    }


def build_report_context(summary, clusters, hosts, vms, charts, logos, config,
                         meta):
    """Assemble the full context dict passed to the report template.

    Args:
        summary: The ``Summary``.
        clusters: List of ``Cluster``.
        hosts: List of ``Host``.
        vms: List of ``VM``.
        charts: The dict returned by ``build_charts``.
        logos: Dict with ``siemens`` / ``nutanix`` data URIs (or None).
        config: Parsed config.
        meta: Dict of run metadata (dates, host, generation time strings).

    Returns:
        A context dict ready for ``render_report``.
    """
    thresholds = config["report"]["thresholds"]
    vm_table_rows = config["report"].get("vm_table_rows", 25)

    # KPI strip values.
    alerts_value = (
        str(summary.critical_alert_count)
        if summary.critical_alert_count is not None
        else "n/a"
    )
    runway_value = (
        "{} days".format(summary.storage_runway_days)
        if summary.storage_runway_days is not None
        else "n/a"
    )
    kpis = [
        {
            "value": "{}/{}".format(
                summary.clusters_healthy, summary.cluster_count
            ),
            "label": "Clusters healthy",
            "color": None,
        },
        {
            "value": str(summary.vm_total),
            "label": "Total VMs ({} on / {} off)".format(
                summary.vm_on, summary.vm_off
            ),
            "color": None,
        },
        {
            "value": alerts_value,
            "label": "Critical alerts (24h)",
            "color": "#D0342C"
            if summary.critical_alert_count
            else None,
        },
        {
            "value": runway_value,
            "label": "Storage runway (worst cluster)",
            "color": None,
        },
    ]

    # Gauges with detail strings.
    gauges = [
        {
            "title": "CPU",
            "img": charts["cpu_gauge"],
            "detail": "{} GHz used of {} GHz".format(
                units.fmt_int(units.hz_to_ghz(summary.cpu_used_hz)),
                units.fmt_int(units.hz_to_ghz(summary.cpu_capacity_hz)),
            ),
        },
        {
            "title": "Memory",
            "img": charts["mem_gauge"],
            "detail": "{} GiB used of {} GiB".format(
                units.fmt_int(summary.mem_used_gib),
                units.fmt_int(summary.mem_capacity_gib),
            ),
        },
        {
            "title": "Storage",
            "img": charts["storage_gauge"],
            "detail": "{} used of {}".format(
                units.fmt_tib(summary.storage_used_bytes),
                units.fmt_tib(summary.storage_total_bytes),
            ),
        },
    ]

    # Efficiency tiles.
    tiles = []
    for (label, bg, light, desc) in _TILE_STYLE:
        if summary.efficiency_available:
            number = str(summary.efficiency_counts.get(label, 0))
        else:
            number = "N/A"
        tiles.append(
            {"n": number, "t": label, "d": desc, "bg": bg, "light": light}
        )
    eff_footnote = (
        None
        if summary.efficiency_available
        else "insufficient baseline data (X-FIT needs ~21 days of history)"
    )

    # Cluster table rows.
    cluster_rows = [_entity_row(cluster, thresholds) for cluster in clusters]

    # Host table grouped by cluster, in cluster order.
    host_groups = []
    for cluster in clusters:
        group_hosts = [h for h in hosts if h.cluster_name == cluster.name]
        host_groups.append(
            {
                "cluster_name": cluster.name,
                "host_count": len(group_hosts),
                "rows": [_entity_row(h, thresholds) for h in group_hosts],
            }
        )

    # VM table: top N by CPU MAX (design document, section 7a).
    vms_ranked = sorted(
        vms,
        key=lambda vm: (vm.cpu_max_pct if vm.stats_ok else -1),
        reverse=True,
    )
    vm_rows = [_vm_row(vm, thresholds) for vm in vms_ranked[:vm_table_rows]]

    return {
        "title": config["branding"]["title"],
        "meta": meta,
        "logos": logos,
        "kpis": kpis,
        "gauges": gauges,
        "thresholds": thresholds,
        "tiles": tiles,
        "eff_footnote": eff_footnote,
        "cpu_bar": charts["cpu_bar"],
        "mem_bar": charts["mem_bar"],
        "top_n": config["report"].get("top_n_vms", 10),
        "cluster_rows": cluster_rows,
        "host_groups": host_groups,
        "vm_rows": vm_rows,
        "vm_shown": len(vm_rows),
        "vm_total": summary.vm_total,
        "vm_stats_failures": summary.vm_stats_failures,
        "footer_contact": config["branding"].get(
            "footer_contact", "Infrastructure Automation team"
        ),
    }


def render_report(context, template_dir):
    """Render the self-contained report HTML from the jinja template.

    Args:
        context: The dict from ``build_report_context``.
        template_dir: Directory holding ``report.html.j2``.

    Returns:
        The rendered HTML as a string.
    """
    environment = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = environment.get_template("report.html.j2")
    return template.render(**context)


def build_email_body(summary, config, meta):
    """Build the compact, image-free HTML email body plus a plain-text version.

    The HTML is table-based with every style inlined for Outlook compatibility
    (design document, section 7b): no images, no CSS blocks, max width 700 px.

    Args:
        summary: The ``Summary``.
        config: Parsed config.
        meta: Run metadata dict.

    Returns:
        A tuple ``(html_string, text_string)``.
    """
    thresholds = config["report"]["thresholds"]

    alerts_value = (
        str(summary.critical_alert_count)
        if summary.critical_alert_count is not None
        else "n/a"
    )
    runway_value = (
        "{} days".format(summary.storage_runway_days)
        if summary.storage_runway_days is not None
        else "n/a"
    )

    # Capacity summary rows: (label, used, total, pct).
    capacity_rows = [
        (
            "CPU",
            "{} GHz".format(units.fmt_int(units.hz_to_ghz(summary.cpu_used_hz))),
            "{} GHz".format(
                units.fmt_int(units.hz_to_ghz(summary.cpu_capacity_hz))
            ),
            summary.cpu_pct,
        ),
        (
            "Memory",
            "{} GiB".format(units.fmt_int(summary.mem_used_gib)),
            "{} GiB".format(units.fmt_int(summary.mem_capacity_gib)),
            summary.mem_pct,
        ),
        (
            "Storage",
            units.fmt_tib(summary.storage_used_bytes),
            units.fmt_tib(summary.storage_total_bytes),
            summary.storage_pct,
        ),
    ]

    eff = summary.efficiency_counts
    if summary.efficiency_available:
        eff_line = "Inactive {i} · Overprovisioned {o} · Constrained {c} · Bully {b}".format(
            i=eff.get("Inactive", 0),
            o=eff.get("Overprovisioned", 0),
            c=eff.get("Constrained", 0),
            b=eff.get("Bully", 0),
        )
    else:
        eff_line = "Efficiency: N/A (insufficient baseline data)"

    report_name = "report_{d}.html".format(d=meta["date_compact"])
    csv_name = "vm_inventory_{d}.csv".format(d=meta["date_compact"])

    html = _render_body_html(
        summary,
        meta,
        alerts_value,
        runway_value,
        capacity_rows,
        thresholds,
        eff_line,
        report_name,
        csv_name,
    )
    text = _render_body_text(
        summary,
        meta,
        alerts_value,
        runway_value,
        capacity_rows,
        eff_line,
        report_name,
        csv_name,
    )
    return html, text


def _render_body_html(summary, meta, alerts_value, runway_value,
                      capacity_rows, thresholds, eff_line, report_name,
                      csv_name):
    """Assemble the inlined-style, table-based HTML email body."""
    # KPI cells.
    kpi_cells = "".join(
        _kpi_cell(value, label, color)
        for (value, label, color) in [
            ("{}/{}".format(summary.clusters_healthy, summary.cluster_count),
             "Clusters healthy", None),
            (str(summary.vm_total),
             "VMs ({} on / {} off)".format(summary.vm_on, summary.vm_off),
             None),
            (alerts_value, "Critical alerts (24h)",
             "#D0342C" if summary.critical_alert_count else None),
            (runway_value, "Storage runway", None),
        ]
    )

    # Capacity rows.
    capacity_html = ""
    for (label, used, total, pct) in capacity_rows:
        band = units.rag_class(pct, thresholds["amber"], thresholds["red"])
        bg = _RAG_BG[band]
        capacity_html += (
            '<tr>'
            '<td style="padding:6px 8px;border-bottom:1px solid #E1E5EA;'
            'font-family:Arial,sans-serif;font-size:13px;color:#131313;">'
            '{label}</td>'
            '<td style="padding:6px 8px;border-bottom:1px solid #E1E5EA;'
            'text-align:right;font-family:Arial,sans-serif;font-size:13px;'
            'color:#131313;">{used}</td>'
            '<td style="padding:6px 8px;border-bottom:1px solid #E1E5EA;'
            'text-align:right;font-family:Arial,sans-serif;font-size:13px;'
            'color:#131313;">{total}</td>'
            '<td style="padding:6px 8px;border-bottom:1px solid #E1E5EA;'
            'text-align:right;font-family:Arial,sans-serif;font-size:13px;'
            'font-weight:bold;color:#ffffff;background:{bg};">{pct}%</td>'
            '</tr>'
        ).format(
            label=label, used=used, total=total, bg=bg,
            pct=units.pct_round(pct)
        )

    return """\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#ECEDF1;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
style="background:#ECEDF1;">
<tr><td align="center" style="padding:16px 8px;">
<table role="presentation" width="700" cellpadding="0" cellspacing="0" \
style="width:700px;max-width:700px;background:#ffffff;">
  <tr><td style="height:4px;background:#7855FA;font-size:0;line-height:0;">\
&nbsp;</td></tr>
  <tr><td style="background:#131313;padding:16px 20px;">
    <div style="font-family:Arial,sans-serif;font-size:17px;color:#ffffff;">\
{title}</div>
    <div style="font-family:Arial,sans-serif;font-size:12px;color:#A9A9B2;\
padding-top:6px;">Report date: {date_long} &nbsp;|&nbsp; Period: last 24 h \
&nbsp;|&nbsp; {clusters} clusters · {hosts} hosts · {vms} VMs</div>
  </td></tr>
  <tr><td style="padding:16px 20px 4px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>{kpi_cells}</tr>
    </table>
  </td></tr>
  <tr><td style="padding:12px 20px 4px;">
    <div style="font-family:Arial,sans-serif;font-size:12px;font-weight:bold;\
color:#5D5D5D;text-transform:uppercase;letter-spacing:1px;padding-bottom:6px;">\
Capacity vs. Consumed (all clusters)</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
style="border-collapse:collapse;">
      <tr>
        <th style="text-align:left;padding:6px 8px;background:#131313;\
color:#ffffff;font-family:Arial,sans-serif;font-size:12px;font-weight:normal;">\
Resource</th>
        <th style="text-align:right;padding:6px 8px;background:#131313;\
color:#ffffff;font-family:Arial,sans-serif;font-size:12px;font-weight:normal;">\
Used</th>
        <th style="text-align:right;padding:6px 8px;background:#131313;\
color:#ffffff;font-family:Arial,sans-serif;font-size:12px;font-weight:normal;">\
Total</th>
        <th style="text-align:right;padding:6px 8px;background:#131313;\
color:#ffffff;font-family:Arial,sans-serif;font-size:12px;font-weight:normal;">\
%</th>
      </tr>
      {capacity_html}
    </table>
  </td></tr>
  <tr><td style="padding:12px 20px 4px;font-family:Arial,sans-serif;\
font-size:12px;color:#5D5D5D;">
    <b style="color:#131313;">VM Efficiency:</b> {eff_line}
  </td></tr>
  <tr><td style="padding:10px 20px 18px;font-family:Arial,sans-serif;\
font-size:12px;color:#131313;">
    Full report attached: <b>{report_name}</b><br>
    VM inventory: <b>{csv_name}</b>
  </td></tr>
  <tr><td style="height:4px;background:#7855FA;font-size:0;line-height:0;">\
&nbsp;</td></tr>
  <tr><td style="background:#F4F4F7;padding:12px 20px;font-family:Arial,\
sans-serif;font-size:11px;color:#6B7480;">
    Generated {generated} by ntnx-daily-report. Distribution: {contact}.
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>""".format(
        title=meta["title"],
        date_long=meta["date_long"],
        clusters=summary.cluster_count,
        hosts=summary.host_count,
        vms=summary.vm_total,
        kpi_cells=kpi_cells,
        capacity_html=capacity_html,
        eff_line=eff_line,
        report_name=report_name,
        csv_name=csv_name,
        generated=meta["generated"],
        contact=meta["contact"],
    )


def _kpi_cell(value, label, color):
    """Render one KPI cell for the email body table."""
    value_color = color if color else "#131313"
    return (
        '<td width="25%" style="padding:4px;">'
        '<table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0" style="border:1px solid #E1E5EA;'
        'border-top:3px solid #7855FA;">'
        '<tr><td style="padding:8px 10px;">'
        '<div style="font-family:Arial,sans-serif;font-size:20px;'
        'font-weight:bold;color:{value_color};">{value}</div>'
        '<div style="font-family:Arial,sans-serif;font-size:10px;'
        'color:#6B7480;text-transform:uppercase;padding-top:2px;">{label}</div>'
        '</td></tr></table></td>'
    ).format(value_color=value_color, value=value, label=label)


def _render_body_text(summary, meta, alerts_value, runway_value,
                     capacity_rows, eff_line, report_name, csv_name):
    """Assemble the plain-text alternative of the email body."""
    lines = []
    lines.append(meta["title"])
    lines.append("Report date: {d}".format(d=meta["date_long"]))
    lines.append(
        "{c} clusters | {h} hosts | {v} VMs ({on} on / {off} off)".format(
            c=summary.cluster_count,
            h=summary.host_count,
            v=summary.vm_total,
            on=summary.vm_on,
            off=summary.vm_off,
        )
    )
    lines.append("")
    lines.append("Clusters healthy: {}/{}".format(
        summary.clusters_healthy, summary.cluster_count))
    lines.append("Critical alerts (24h): {}".format(alerts_value))
    lines.append("Storage runway (worst cluster): {}".format(runway_value))
    lines.append("")
    lines.append("Capacity vs. Consumed (all clusters):")
    for (label, used, total, pct) in capacity_rows:
        lines.append("  {label}: {used} of {total} ({pct}%)".format(
            label=label, used=used, total=total, pct=units.pct_round(pct)))
    lines.append("")
    lines.append("VM Efficiency: {}".format(eff_line))
    lines.append("")
    lines.append("Full report attached: {}".format(report_name))
    lines.append("VM inventory: {}".format(csv_name))
    lines.append("")
    lines.append("Generated {g} by ntnx-daily-report.".format(
        g=meta["generated"]))
    return "\n".join(lines)
