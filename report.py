"""ntnx-daily-report entry point.

Collects resource, capacity and efficiency data from Nutanix Prism Central,
renders a self-contained HTML report plus a compact email summary and a VM
inventory CSV, and either writes them to ``./out`` (``--dry-run``) or emails
them to the configured distribution list.

Command-line contract (design document, section 4):

    python report.py                  collect -> render -> email
    python report.py --dry-run        collect + render, write ./out, do NOT email
    python report.py --mock           use fixtures/sample_data.json (offline)
    python report.py --config PATH    use an alternate config file
    python report.py --to a@b.com     override recipients (repeatable)

``--mock --dry-run`` together run fully offline and are how the report is
developed and its layout tested without Prism Central access.

Design principles honoured here:
  * The report must still go out on partial failure. Only a total failure of
    cluster inventory aborts the run, and even then a failure-notification email
    is sent to the same list and the process exits non-zero (section 11).
  * Secrets come from the environment, never from config on disk (section 10).
"""

import argparse
import datetime
import logging
import logging.handlers
import os
import sys

import yaml

import interactive
from collector import clusters as clusters_mod
from collector import efficiency as efficiency_mod
from collector import hosts as hosts_mod
from collector import summarize
from collector import vms as vms_mod
from collector.client import MockClient, PrismClient, PrismApiError
from render import html as html_mod
from render.csvout import write_vm_inventory_csv, CSV_HEADER, vm_to_csv_row

# Directory of this file — used to resolve resources regardless of the working
# directory cron launches us from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Path handling for two runtime modes:
#   * Normal Python run (development / cron): everything lives under BASE_DIR,
#     exactly as before — the two dirs below both equal BASE_DIR so behavior is
#     unchanged.
#   * Frozen single-file executable (PyInstaller, Windows .exe): read-only
#     bundled resources (the Jinja template and the mock fixtures) are unpacked
#     to a temporary directory (sys._MEIPASS), while operator-facing files
#     (config.yaml, .env, assets/, and the out/ and logs/ output) live next to
#     the .exe so they can be edited and kept.
if getattr(sys, "frozen", False):
    RUNTIME_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_DIR = getattr(sys, "_MEIPASS", RUNTIME_DIR)
else:
    RUNTIME_DIR = BASE_DIR
    RESOURCE_DIR = BASE_DIR

LOG = logging.getLogger("ntnx")


# ---------------------------------------------------------------------------
# Configuration and secrets
# ---------------------------------------------------------------------------
def load_env_file(path):
    """Load simple KEY=VALUE lines from a .env file into ``os.environ``.

    Parsed manually so the project needs no python-dotenv dependency. Existing
    environment variables are NOT overwritten (a real exported value wins over
    the file). Missing file is a no-op.

    Args:
        path: Path to the .env file.
    """
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_config(config_path):
    """Load and return the YAML configuration as a dict.

    Args:
        config_path: Path to config.yaml.

    Returns:
        The parsed configuration dict.
    """
    with open(config_path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging():
    """Configure logging to stdout and a 14-day rotating file.

    INFO and above go to stdout; DEBUG and above go to ``./logs/report.log``
    (design document, section 11). Credentials are never logged by any module.
    """
    logs_dir = os.path.join(RUNTIME_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    root = logging.getLogger("ntnx")
    root.setLevel(logging.DEBUG)
    # Avoid duplicate handlers if setup_logging is called more than once.
    if root.handlers:
        return

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        os.path.join(logs_dir, "report.log"),
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Run metadata and reporting window
# ---------------------------------------------------------------------------
def build_window_and_meta(config):
    """Compute the reporting window params and the display metadata.

    Args:
        config: The parsed config dict.

    Returns:
        A tuple ``(window, meta)``. ``window`` holds the stats query ``params``;
        ``meta`` holds display strings used by the templates.
    """
    window_hours = config["report"].get("window_hours", 24)
    tz_name = config["report"].get("timezone", "Asia/Kolkata")
    # Buffer the end time a few seconds into the past. The v4 stats API does a
    # strict microsecond comparison against the server clock and rejects any
    # end time that is even milliseconds ahead of it (error CLU-10006). A small
    # buffer absorbs client/server clock skew.
    buffer_seconds = config["report"].get("time_buffer_seconds", 120)

    # Local time is used only for the human-readable display in the report.
    now = _now_in_timezone(tz_name)
    start = now - datetime.timedelta(hours=window_hours)

    # The API window is computed in UTC with the safety buffer applied, because
    # the stats endpoints expect UTC and compare against the server's UTC clock.
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    api_end = now_utc - datetime.timedelta(seconds=buffer_seconds)
    api_start = api_end - datetime.timedelta(hours=window_hours)

    # Short timezone label for display (falls back to the config name).
    tz_label = now.strftime("%Z") or tz_name

    # Note: $select is per-endpoint (each metric set differs) and is added by
    # each collector; only the shared time params live here.
    window = {
        "params": {
            "$startTime": _to_utc_z(api_start),
            "$endTime": _to_utc_z(api_end),
            "$samplingInterval": 300,
            "$statType": "AVG",
        }
    }

    meta = {
        "title": config["branding"]["title"],
        "date_long": now.strftime("%A, %d %B %Y"),
        "date_compact": now.strftime("%Y%m%d"),
        "window_hours": window_hours,
        "window_desc": "last {h} hours ({start} → {end} {tz})".format(
            h=window_hours,
            start=start.strftime("%d-%b %H:%M"),
            end=now.strftime("%d-%b %H:%M"),
            tz=tz_label,
        ),
        "pc_host": config["prism_central"]["host"],
        "generated": now.strftime("%H:%M ") + tz_label + now.strftime(" on %d-%b-%Y"),
        "gen_host": _hostname(),
        "contact": ", ".join(config["smtp"]["recipients"]),
    }
    return window, meta


def _now_in_timezone(tz_name):
    """Return the current time in the given IANA timezone, best-effort.

    Uses ``zoneinfo`` when the timezone database is available; otherwise falls
    back to naive local time so the run never fails over a missing tzdata.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.datetime.now(ZoneInfo(tz_name))
    except Exception:
        LOG.debug("zoneinfo unavailable for %s; using local time", tz_name)
        return datetime.datetime.now()


def _to_utc_z(moment):
    """Format a datetime as a UTC RFC-3339 string with a trailing ``Z``.

    Microseconds are dropped: the API compares at microsecond precision, so a
    whole-second timestamp avoids spurious "time in future" rejections.

    Args:
        moment: A timezone-aware datetime (assumed UTC) or naive datetime.

    Returns:
        A string such as ``"2026-08-11T05:34:00Z"``.
    """
    if moment.tzinfo is not None:
        moment = moment.astimezone(datetime.timezone.utc)
    return moment.replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _hostname():
    """Return the local hostname for the footer, best-effort."""
    try:
        import socket

        return socket.gethostname()
    except Exception:
        return "management-host"


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
def collect_all(client, config, window):
    """Run every collector and return the assembled report data.

    Cluster inventory failure raises (the caller turns it into a failure email
    and non-zero exit). Every other collector degrades gracefully.

    Args:
        client: A PrismClient or MockClient.
        config: The parsed config dict.
        window: The stats window dict.

    Returns:
        A tuple ``(clusters, hosts, vms, summary)``.

    Raises:
        Exception: If cluster inventory collection fails entirely.
    """
    # --- Cluster inventory (the only hard-fail point) ------------------------
    raw_clusters = clusters_mod.collect_cluster_inventory(client)
    if not raw_clusters:
        raise PrismApiError(
            "cluster inventory returned no AOS clusters to report on"
        )

    # --- Hosts per cluster, then build the Cluster records -------------------
    all_hosts = []
    cluster_records = []
    cluster_name_by_id = {}
    for raw in raw_clusters:
        cluster_ext_id = raw.get("extId")
        cluster_name = raw.get("name") or cluster_ext_id
        cluster_name_by_id[cluster_ext_id] = cluster_name

        cluster_hosts = hosts_mod.collect_hosts(
            client, cluster_ext_id, cluster_name, window
        )
        all_hosts.extend(cluster_hosts)

        cluster_records.append(
            clusters_mod.build_cluster(client, raw, cluster_hosts, window)
        )

    # --- Efficiency + guest storage (estate-wide, soft) ----------------------
    eff_map, eff_available = efficiency_mod.collect_efficiency_by_vm(client)
    guest_map = efficiency_mod.collect_guest_storage_by_vm(client)

    # --- VMs (with stats, guest and efficiency merged) -----------------------
    vm_records, vm_failures = vms_mod.collect_vms(
        client, cluster_name_by_id, window, config, guest_map, eff_map
    )

    # --- Optional KPI values (soft) ------------------------------------------
    critical_alerts = efficiency_mod.collect_critical_alert_count(client)
    runway_days = efficiency_mod.collect_storage_runway_days(client)

    # --- Summary roll-up -----------------------------------------------------
    summary = summarize.build_summary(
        cluster_records,
        all_hosts,
        vm_records,
        eff_map,
        eff_available,
        critical_alerts,
        runway_days,
        vm_failures,
    )
    return cluster_records, all_hosts, vm_records, summary


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_all(clusters, hosts, vms, summary, config, meta):
    """Render the report HTML, the email body, the text body and the CSV bytes.

    Args:
        clusters, hosts, vms: Collected records.
        summary: The ``Summary``.
        config: The parsed config dict.
        meta: The run metadata dict.

    Returns:
        A tuple ``(report_html, body_html, body_text, csv_bytes)``.
    """
    assets_dir = _resolve_path(config["branding"].get("assets_dir", "./assets"))
    logos = {
        "siemens": html_mod.embed_image_file(
            os.path.join(assets_dir, "siemens_logo.png")
        ),
        "nutanix": html_mod.embed_image_file(
            os.path.join(assets_dir, "nutanix_logo.png")
        ),
    }
    for name in ("siemens", "nutanix"):
        if logos[name] is None:
            LOG.info("Logo '%s' not found; omitting its <img> cleanly", name)

    charts = html_mod.build_charts(summary, vms, config)
    context = html_mod.build_report_context(
        summary, clusters, hosts, vms, charts, logos, config, meta
    )
    template_dir = os.path.join(RESOURCE_DIR, "render", "templates")
    report_html = html_mod.render_report(context, template_dir)

    body_html, body_text = html_mod.build_email_body(summary, config, meta)

    csv_bytes = _csv_bytes(vms)
    return report_html, body_html, body_text, csv_bytes


def _csv_bytes(vms):
    """Render the full VM inventory CSV to a UTF-8 bytes object."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADER)
    for vm in vms:
        writer.writerow(vm_to_csv_row(vm))
    return buffer.getvalue().encode("utf-8")


def _resolve_path(path):
    """Resolve a possibly-relative config path against the project directory."""
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(RUNTIME_DIR, path))


# ---------------------------------------------------------------------------
# Output: dry-run files and email
# ---------------------------------------------------------------------------
def write_dry_run_outputs(report_html, body_html, csv_bytes, vms, meta):
    """Write the report, email body and CSV to ``./out`` for offline review.

    Args:
        report_html: The self-contained report HTML.
        body_html: The email body HTML.
        csv_bytes: The CSV content bytes.
        vms: The VM records (written via the CSV writer for a real file).
        meta: The run metadata dict.

    Returns:
        A dict of the written file paths.
    """
    out_dir = os.path.join(RUNTIME_DIR, "out")
    os.makedirs(out_dir, exist_ok=True)

    report_path = os.path.join(
        out_dir, "report_{d}.html".format(d=meta["date_compact"])
    )
    body_path = os.path.join(out_dir, "email_body.html")
    csv_path = os.path.join(
        out_dir, "vm_inventory_{d}.csv".format(d=meta["date_compact"])
    )

    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(report_html)
    with open(body_path, "w", encoding="utf-8") as handle:
        handle.write(body_html)
    row_count = write_vm_inventory_csv(csv_path, vms)

    LOG.info("Dry-run wrote:")
    LOG.info("  %s", report_path)
    LOG.info("  %s", body_path)
    LOG.info("  %s (%d VM rows)", csv_path, row_count)
    return {"report": report_path, "body": body_path, "csv": csv_path}


def build_subject(summary, meta):
    """Build the subject line (design document, section 7b)."""
    subject = "[Nutanix Daily Report] Siemens Ltd — {date} — {c} clusters, {v} VMs".format(
        date=meta["date_compact"][:4]
        + "-"
        + meta["date_compact"][4:6]
        + "-"
        + meta["date_compact"][6:],
        c=summary.cluster_count,
        v=summary.vm_total,
    )
    if summary.critical_alert_count:
        subject += ", ⚠ {k} critical alerts".format(
            k=summary.critical_alert_count
        )
    return subject


def send_email(config, summary, meta, report_html, body_html, body_text,
               csv_bytes, recipients_override, smtp_password=None):
    """Send the assembled report via SMTP.

    Args:
        config: The parsed config dict.
        summary: The ``Summary`` (for the subject line).
        meta: The run metadata dict.
        report_html, body_html, body_text: Rendered content.
        csv_bytes: The CSV attachment bytes.
        recipients_override: A list of recipients from ``--to``, or ``None`` to
            use the configured distribution list.
    """
    import mailer

    smtp_config = config["smtp"]
    recipients = recipients_override or smtp_config["recipients"]
    cc = smtp_config.get("cc") or []
    reply_to = smtp_config.get("reply_to")

    subject = build_subject(summary, meta)
    report_filename = "report_{d}.html".format(d=meta["date_compact"])
    csv_filename = "vm_inventory_{d}.csv".format(d=meta["date_compact"])

    message = mailer.build_message(
        subject=subject,
        from_addr=smtp_config["from"],
        recipients=recipients,
        body_html=body_html,
        body_text=body_text,
        report_html=report_html,
        report_filename=report_filename,
        csv_bytes=csv_bytes,
        csv_filename=csv_filename,
        cc=cc,
        reply_to=reply_to,
    )
    # Prefer an explicitly resolved password (interactive prompt); fall back to
    # the environment for the unattended path.
    if smtp_password is None:
        smtp_password = os.environ.get("SMTP_PASSWORD")
    mailer.send_message(
        smtp_config, message, list(recipients) + list(cc), password=smtp_password
    )


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------
def build_client(config, use_mock, password=None):
    """Construct either a MockClient or a live PrismClient.

    Args:
        config: The parsed config dict (host/username already resolved).
        use_mock: ``True`` to serve canned fixtures offline.
        password: The Prism Central password (already resolved from env or an
            interactive prompt). Required for a live client.

    Returns:
        A client exposing get_json / post_json / paginate_v4.
    """
    if use_mock:
        fixture_path = os.path.join(
            RESOURCE_DIR, "fixtures", "sample_data.json"
        )
        return MockClient(fixture_path)

    pc_config = config["prism_central"]
    return PrismClient(
        host=pc_config["host"],
        port=pc_config["port"],
        username=pc_config["username"],
        password=password,
        verify_ssl=pc_config.get("verify_ssl", False),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv):
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate and send the Nutanix daily resource & capacity "
        "report for Siemens Limited."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect + render, write files to ./out, do not email",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="use fixtures/sample_data.json instead of a live Prism Central",
    )
    parser.add_argument(
        "--config",
        default=os.path.join(RUNTIME_DIR, "config.yaml"),
        help="path to an alternate config file",
    )
    parser.add_argument(
        "--to",
        action="append",
        default=None,
        help="override recipient (repeatable); for testing",
    )
    return parser.parse_args(argv)


def main(argv=None):
    """Program entry point. Returns a process exit code."""
    args = parse_args(argv if argv is not None else sys.argv[1:])

    setup_logging()

    # Load secrets from an optional .env next to the config, then config.
    load_env_file(os.path.join(RUNTIME_DIR, ".env"))
    config = load_config(args.config)

    window, meta = build_window_and_meta(config)

    LOG.info(
        "ntnx-daily-report starting (mock=%s, dry_run=%s)",
        args.mock,
        args.dry_run,
    )

    # --- Prism Central connection (prompt if the config is left blank) --------
    pc_password = None
    if not args.mock:
        interactive.resolve_prism_settings(config)
        pc_password = interactive.resolve_prism_password(config)

    client = build_client(config, args.mock, password=pc_password)

    # --- Collection: only total cluster-inventory failure aborts -------------
    try:
        clusters, hosts, vms, summary = collect_all(client, config, window)
    except Exception as exc:
        LOG.error("FATAL: cluster inventory collection failed: %s", exc)
        # Only try to email a failure notice on the unattended, SMTP-configured
        # path; an ad-hoc operator sees the error on their terminal.
        if not args.dry_run and interactive.smtp_is_configured(config):
            _notify_failure(config, meta, str(exc), args.to)
        return 1

    # --- Rendering -----------------------------------------------------------
    report_html, body_html, body_text, csv_bytes = render_all(
        clusters, hosts, vms, summary, config, meta
    )

    # --- Delivery decision ---------------------------------------------------
    # --dry-run and --mock never email: they just write the files to ./out.
    if args.dry_run or args.mock:
        write_dry_run_outputs(report_html, body_html, csv_bytes, vms, meta)
        LOG.info("Files written to ./out; no email sent.")
        return 0

    send_email_now, smtp_password = _decide_delivery(config, args)
    if not send_email_now:
        write_dry_run_outputs(report_html, body_html, csv_bytes, vms, meta)
        print(
            "\nReport generated in ./out (not emailed). Open "
            "out/report_{d}.html to view it.".format(d=meta["date_compact"])
        )
        return 0

    try:
        send_email(
            config, summary, meta, report_html, body_html, body_text,
            csv_bytes, args.to, smtp_password=smtp_password
        )
    except Exception as exc:
        LOG.error("Report generated but sending failed: %s", exc)
        # Don't lose the work: still write the files locally.
        write_dry_run_outputs(report_html, body_html, csv_bytes, vms, meta)
        return 1

    LOG.info("Done.")
    return 0


def _decide_delivery(config, args):
    """Decide whether to email the report, prompting when SMTP is unconfigured.

    Args:
        config: The parsed config dict (SMTP may be filled in interactively).
        args: Parsed CLI args.

    Returns:
        A tuple ``(send_email, smtp_password)``. ``send_email`` is ``False`` when
        the report should just be written to ./out.
    """
    # Fully configured SMTP (cron path): send without asking.
    if interactive.smtp_is_configured(config):
        return True, os.environ.get("SMTP_PASSWORD")

    # Not configured. If a terminal is attached, offer to set it up now.
    if interactive.is_interactive():
        if interactive.prompt_yes_no(
            "\nSend the report by email now?", default=False
        ):
            smtp_password = interactive.prompt_smtp_settings(config)
            if smtp_password is None:
                smtp_password = os.environ.get("SMTP_PASSWORD")
            return True, smtp_password
        return False, None

    # No SMTP config and no terminal: just write the files.
    LOG.warning(
        "SMTP is not configured and no terminal is available to prompt; "
        "writing the report to ./out instead of emailing."
    )
    return False, None


def _notify_failure(config, meta, reason, recipients_override):
    """Best-effort failure-notification email (never raises)."""
    try:
        import mailer

        recipients = recipients_override or config["smtp"]["recipients"]
        mailer.send_failure_notification(
            config["smtp"],
            recipients,
            reason,
            password=os.environ.get("SMTP_PASSWORD"),
        )
        LOG.info("Failure notification sent to %s", ", ".join(recipients))
    except Exception as exc:
        LOG.error("Could not send failure notification: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
