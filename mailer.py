"""SMTP delivery of the daily report.

Builds the v1.1 MIME structure (design document, section 9) — a compact,
image-free summary as the body, with the self-contained HTML report and the VM
inventory CSV as attachments — and sends it through the configured mail relay:

    multipart/mixed
    ├── multipart/alternative
    │   ├── text/plain            (summary)
    │   └── text/html             (summary body, no images)
    ├── text/html attachment      report_{YYYYMMDD}.html
    └── text/csv attachment       vm_inventory_{YYYYMMDD}.csv

Supports plain (port 25), STARTTLS (587) and SSL (465) relays, and anonymous
relays (no login when no username is configured). Passwords are never logged.
"""

import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

LOG = logging.getLogger("ntnx.mailer")


def build_message(
    subject,
    from_addr,
    recipients,
    body_html,
    body_text,
    report_html,
    report_filename,
    csv_bytes,
    csv_filename,
    cc=None,
    reply_to=None,
):
    """Build the complete MIME message (not sent here).

    Args:
        subject: The subject line.
        from_addr: The From header value.
        recipients: List of To recipients.
        body_html: The image-free HTML summary body.
        body_text: The plain-text alternative body.
        report_html: The self-contained report HTML (attachment content).
        report_filename: Filename for the report attachment.
        csv_bytes: The CSV attachment content as bytes.
        csv_filename: Filename for the CSV attachment.
        cc: Optional list of Cc recipients.
        reply_to: Optional Reply-To address.

    Returns:
        A ``MIMEMultipart`` ("mixed") message ready to send.
    """
    message = MIMEMultipart("mixed")
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = ", ".join(recipients)
    if cc:
        message["Cc"] = ", ".join(cc)
    if reply_to:
        message["Reply-To"] = reply_to
    message["Date"] = formatdate(localtime=True)

    # The alternative part carries the text and HTML summaries.
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(body_text, "plain", "utf-8"))
    alternative.attach(MIMEText(body_html, "html", "utf-8"))
    message.attach(alternative)

    # The self-contained HTML report attachment.
    report_part = MIMEApplication(
        report_html.encode("utf-8"), _subtype="octet-stream"
    )
    report_part.add_header(
        "Content-Disposition", "attachment", filename=report_filename
    )
    # Present it as text/html so mail clients preview it as a web page.
    report_part.replace_header("Content-Type", "text/html; charset=utf-8")
    message.attach(report_part)

    # The VM inventory CSV attachment.
    csv_part = MIMEApplication(csv_bytes, _subtype="csv")
    csv_part.add_header(
        "Content-Disposition", "attachment", filename=csv_filename
    )
    csv_part.replace_header("Content-Type", "text/csv; charset=utf-8")
    message.attach(csv_part)

    return message


def send_message(smtp_config, message, all_recipients, password=None):
    """Send a prepared MIME message through the configured relay.

    Args:
        smtp_config: The ``smtp`` section of the config dict.
        message: The MIME message to send.
        all_recipients: The full envelope recipient list (To + Cc).
        password: SMTP password (from the environment), or ``None`` for an
            anonymous relay.

    Raises:
        Exception: Propagates any SMTP error to the caller.
    """
    host = smtp_config["host"]
    port = smtp_config["port"]
    mode = smtp_config.get("mode", "plain")
    username = smtp_config.get("username")

    from_addr = smtp_config["from"]

    LOG.info(
        "Sending report via %s:%s (mode=%s) to %d recipient(s)",
        host,
        port,
        mode,
        len(all_recipients),
    )

    if mode == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)

    try:
        server.ehlo()
        if mode == "starttls":
            server.starttls()
            server.ehlo()
        # Only authenticate when a username is configured (anonymous relay
        # otherwise, per the design document, section 9).
        if username:
            server.login(username, password or "")
        server.sendmail(
            from_addr, all_recipients, message.as_string()
        )
        LOG.info("Report sent successfully")
    finally:
        try:
            server.quit()
        except Exception:
            # A relay that drops the connection on quit is not a send failure.
            pass


def send_failure_notification(smtp_config, recipients, reason, password=None):
    """Send a plain-text failure notice so silence never masks a failure.

    Used when cluster inventory collection fails entirely (design document,
    section 11): the report cannot be built, so a short notification goes to the
    same distribution list.

    Args:
        smtp_config: The ``smtp`` section of the config dict.
        recipients: The recipient list.
        reason: A short human-readable failure reason.
        password: SMTP password, or ``None``.
    """
    message = MIMEText(
        "The Nutanix daily report could not be generated.\n\n"
        "Reason: {reason}\n\n"
        "This is an automated failure notice from ntnx-daily-report so that a "
        "missing report is never mistaken for a healthy estate.".format(
            reason=reason
        ),
        "plain",
        "utf-8",
    )
    message["Subject"] = "[Nutanix Daily Report] GENERATION FAILED"
    message["From"] = smtp_config["from"]
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)

    send_message(smtp_config, message, recipients, password=password)
