"""Interactive prompts for ad-hoc, run-it-once execution.

When ``config.yaml`` is left with blank Prism Central / SMTP settings (the
"run it locally once a month" case rather than a scheduled cron job), the
script asks the operator for the connection details at runtime instead of
failing. This module holds the small, dependency-free prompt helpers and the
two composed flows the entry point uses.

Design rules:
  * Environment variables always win over a prompt (so cron stays unattended).
  * Prompts only happen when a real terminal is attached; otherwise the caller
    raises a clear error rather than blocking forever on ``input()``.
  * The password prompt never echoes (uses ``getpass``) and is never logged.
"""

import getpass
import logging
import os
import sys

LOG = logging.getLogger("ntnx.interactive")

# Sample/placeholder values that should be treated as "not really filled in".
_PC_PLACEHOLDERS = frozenset(
    {"pc.siemens.internal", "<pc-ip>", "<pc>", "changeme"}
)
_SMTP_PLACEHOLDERS = frozenset({"mailrelay.siemens.internal"})

# Default service-account name offered at the username prompt.
_DEFAULT_USERNAME = "svc_report_ro"


def is_interactive():
    """Return True when both stdin and stdout are attached to a terminal."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def is_blank(value, placeholders=frozenset()):
    """Return True when a config value is unset, empty, or a known placeholder.

    Args:
        value: The config value to test.
        placeholders: Extra placeholder strings that count as "blank".

    Returns:
        ``True`` when the value should be treated as not filled in.
    """
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    lowered = {p.lower() for p in placeholders}
    return text.lower() in lowered


def prompt_text(label, default=None, required=False):
    """Prompt for a line of text.

    Args:
        label: The prompt label shown to the user.
        default: Value returned when the user just presses Enter.
        required: When ``True`` (and no default), keep asking until non-empty.

    Returns:
        The entered string (or the default).
    """
    suffix = " [{d}]".format(d=default) if default else ""
    while True:
        raw = input("{label}{suffix}: ".format(label=label, suffix=suffix))
        raw = raw.strip()
        if not raw and default is not None:
            return default
        if raw:
            return raw
        if not required:
            return ""
        print("  A value is required.")


def prompt_password(label="Password"):
    """Prompt for a secret without echoing it to the screen."""
    return getpass.getpass("{label}: ".format(label=label))


def prompt_yes_no(question, default=False):
    """Prompt a yes/no question and return a bool.

    Args:
        question: The question text.
        default: The value returned on an empty answer.

    Returns:
        ``True`` for yes, ``False`` for no.
    """
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input("{q} [{hint}]: ".format(q=question, hint=hint))
        raw = raw.strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Please answer y or n.")


def resolve_prism_settings(config):
    """Ensure ``prism_central.host``/``username`` are set, prompting if blank.

    Mutates ``config['prism_central']`` in place.

    Args:
        config: The parsed config dict.

    Raises:
        SystemExit: When the host is blank and there is no terminal to prompt.
    """
    pc_config = config["prism_central"]

    if is_blank(pc_config.get("host"), _PC_PLACEHOLDERS):
        if not is_interactive():
            raise SystemExit(
                "prism_central.host is not set in the config and there is no "
                "terminal to prompt. Fill it in config.yaml (and set "
                "PC_PASSWORD) for unattended runs, or run interactively."
            )
        print("\nPrism Central connection")
        pc_config["host"] = prompt_text(
            "  Prism Central IP / FQDN", required=True
        )

    if is_blank(pc_config.get("username")):
        # Offer the conventional read-only account name as the default.
        if is_interactive():
            pc_config["username"] = prompt_text(
                "  Username", default=_DEFAULT_USERNAME
            )
        else:
            pc_config["username"] = _DEFAULT_USERNAME


def resolve_prism_password(config):
    """Return the Prism Central password.

    ``PC_PASSWORD`` from the environment always wins (cron path). Otherwise the
    operator is prompted, if a terminal is available.

    Returns:
        The password string.

    Raises:
        SystemExit: When no password is available and there is no terminal.
    """
    env_password = os.environ.get("PC_PASSWORD")
    if env_password:
        return env_password
    if is_interactive():
        return prompt_password("  Prism Central password")
    raise SystemExit(
        "PC_PASSWORD is not set and there is no terminal to prompt. Export it "
        "or add it to .env for unattended runs."
    )


def smtp_is_configured(config):
    """Return True when SMTP is filled in enough to send without prompting."""
    smtp_config = config["smtp"]
    host_ok = not is_blank(smtp_config.get("host"), _SMTP_PLACEHOLDERS)
    has_recipients = bool(smtp_config.get("recipients"))
    return host_ok and has_recipients


def prompt_smtp_settings(config):
    """Interactively fill in SMTP settings and return the SMTP password (or None).

    Mutates ``config['smtp']`` in place with the entered values.

    Args:
        config: The parsed config dict.

    Returns:
        The SMTP password string when a username was given, else ``None``
        (anonymous relay).
    """
    smtp_config = config["smtp"]
    print("\nEmail delivery details")

    smtp_config["host"] = prompt_text(
        "  SMTP relay host",
        default=(smtp_config.get("host") or None),
        required=True,
    )
    smtp_config["port"] = int(
        prompt_text("  SMTP port", default=str(smtp_config.get("port") or 25))
    )
    smtp_config["mode"] = prompt_text(
        "  Mode (plain / starttls / ssl)",
        default=(smtp_config.get("mode") or "plain"),
    )
    smtp_config["from"] = prompt_text(
        "  From address",
        default=(smtp_config.get("from") or None),
        required=True,
    )
    recipients_raw = prompt_text(
        "  Recipient(s), comma-separated", required=True
    )
    smtp_config["recipients"] = [
        part.strip() for part in recipients_raw.split(",") if part.strip()
    ]

    username = prompt_text(
        "  SMTP username (blank = anonymous relay)", default=""
    )
    smtp_config["username"] = username or None

    smtp_password = None
    if smtp_config["username"]:
        smtp_password = prompt_password("  SMTP password")
    return smtp_password
