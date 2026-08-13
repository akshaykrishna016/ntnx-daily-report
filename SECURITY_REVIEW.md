# Security Review — ntnx-daily-report

**Scope:** the `ntnx-daily-report` Python project (report generator that reads
Nutanix Prism Central over REST and emails a report).
**Review type:** automated SAST + dependency CVE scan + manual code review.
**Overall posture:** low risk. No high/medium automated findings, no known
CVEs in dependencies, no dangerous language features, secrets handled via
environment variables. A small number of hardening items are listed below; the
two worth acting on before a customer deployment are **F1 (CSV formula
injection)** and **F2 (SMTP transport encryption)**.

This document is written to be handed to the customer's security team. Section 7
tells them how to reproduce every check themselves.

---

## 1. How this was verified

| Check | Tool | Result |
|---|---|---|
| Static analysis (SAST) | `bandit -r .` | 1 Low (benign), 0 Medium, 0 High |
| Dependency vulnerabilities | `pip-audit -r requirements.txt` | No known vulnerabilities |
| Dangerous constructs | manual + grep (`eval`, `exec`, `os.system`, `subprocess`, `shell=True`, `pickle`, `yaml.load`) | None present |
| Secrets in source | grep of `config.yaml`, `.env.example`, all `.py` | None (placeholders only) |
| YAML parsing | manual | `yaml.safe_load` only (no arbitrary object construction) |
| Template injection | manual | Jinja2 autoescape enabled for HTML |
| Credential logging | manual | Passwords / `Authorization` never logged |
| Runtime egress | manual | Only Prism Central (9440) + SMTP relay; no CDN/telemetry |

---

## 2. Findings summary

| ID | Severity | Area | Status |
|---|---|---|---|
| F1 | Medium | CSV formula injection (Excel) | Open — fix recommended before deploy |
| F2 | Medium | SMTP sends report in cleartext in `plain` mode | Config choice — recommend STARTTLS/SSL |
| F3 | Low | TLS verification disabled by default (`verify_ssl: false`) | Acceptable for self-signed; recommend CA bundle in prod |
| F4 | Low | `.env` file permissions not enforced | Operational hardening |
| F5 | Low | `try/except/pass` on SMTP quit (Bandit B110) | Cosmetic |
| F6 | Info | Secrets reside in process environment | Documented; standard practice |
| F7 | Info | Report content is mildly sensitive (hostnames, IPs, capacity) | Handle per data-classification policy |

---

## 3. Detailed findings

### F1 — CSV formula injection (Medium, CWE-1236)

`render/csvout.py` writes VM names (and other values that originate from the
Prism Central API) into the inventory CSV using `csv.writer`. `csv.writer`
correctly quotes delimiters, but it does **not** neutralize spreadsheet formula
triggers. If a VM were named, e.g., `=HYPERLINK("http://evil","click")` or
`=cmd|'/c calc'!A1`, opening the CSV in Excel could execute it.

Risk is bounded (an attacker would need the ability to name VMs in the estate),
but customer security teams routinely flag this for any CSV a human opens.

**Remediation:** prefix any cell that begins with `=`, `+`, `-`, `@`, tab, or
carriage return with a single quote (`'`) before writing. This is a ~5-line
change to `vm_to_csv_row` / the writer. I can apply it on request.

### F2 — SMTP transport encryption (Medium)

`config.yaml` supports `smtp.mode: plain | starttls | ssl`. In `plain` mode
(port 25) the message — which contains internal hostnames, hypervisor IPs and
capacity data — traverses the relay unencrypted. `mailer.py` correctly
implements STARTTLS and SSL; this is purely a configuration choice.

**Remediation:** use `starttls` (587) or `ssl` (465) to the relay in production.
Only fall back to `plain` if the relay is on a trusted, isolated management
segment and policy allows it.

### F3 — TLS verification disabled by default (Low, CWE-295)

`prism_central.verify_ssl` defaults to `false`, which disables certificate
verification for the Prism Central connection and (correctly, only in this case)
suppresses the urllib3 InsecureRequestWarning. This is the documented, common
setup because Prism Central ships a self-signed certificate, but it leaves the
PC connection susceptible to man-in-the-middle on the management network.

**Remediation:** for production, set `verify_ssl` to the path of the customer's
CA bundle (the config and client already support
`verify_ssl: /path/to/ca.pem`). The code path that suppresses the TLS warning is
reached **only** when verification is explicitly disabled, so enabling a CA
bundle restores full verification with no code change.

### F4 — `.env` file permissions (Low)

Secrets can be supplied via a `.env` file that `report.py` parses manually. The
code does not create or enforce restrictive permissions on that file.

**Remediation:** `chmod 600 .env`, own it by the service account, and keep it
out of version control (add `.env` to `.gitignore`). Prefer injecting
`PC_PASSWORD` / `SMTP_PASSWORD` from the platform's secret store (systemd
`LoadCredential`, Vault, etc.) over a file on disk where possible.

### F5 — `try/except/pass` on SMTP quit (Low, Bandit B110)

`mailer.py` swallows exceptions from `server.quit()`. This is intentional (a
relay dropping the connection on quit is not a send failure) and carries no
security impact. Optional: log at DEBUG instead of `pass` for auditability.

### F6 — Secrets in process environment (Info)

`PC_PASSWORD` / `SMTP_PASSWORD` live in the process environment at runtime,
readable by the same user and root via `/proc`. This is standard and acceptable;
documented here so the security team is aware. Mitigations: run under a
dedicated low-privilege service account; avoid dumping the environment in any
debugging.

### F7 — Report content sensitivity (Info)

Generated artifacts (the HTML report and CSV) contain internal hostnames,
hypervisor IP addresses and capacity figures. Treat them per the customer's data
classification policy: restrict the output directory, the mailbox/PDL, and any
place the files are archived.

---

## 4. Controls already in place (what's done well)

- **No dangerous constructs:** no `eval`/`exec`/`os.system`/`subprocess`/
  `shell=True`/`pickle`; YAML parsed with `safe_load`; JSON only.
- **Secrets never in source or logs:** no credentials in `config.yaml` or the
  repo; passwords come only from env; the `Authorization` header and passwords
  are never logged (DEBUG logs are limited to method, URL, status, elapsed).
- **Least privilege by design:** intended to run with a **read-only** Prism
  Central service account; the tool performs only GETs and read-only v3 groups
  POSTs — no create/update/delete.
- **Output/template safety:** the attached report is rendered with Jinja2
  autoescaping, so entity names from the API cannot inject HTML/script.
- **Minimal, self-contained runtime:** four well-known dependencies; the report
  embeds all assets as base64 with **zero external references** (no CDN, no
  outbound calls at render time) — friendly to air-gapped/egress-restricted
  networks.
- **Bounded resource use:** 30 s timeouts, capped retries with backoff, and a
  max-8-worker thread pool; a single entity's failure cannot hang or abort the
  run.

---

## 5. Pre-deployment hardening checklist

- [ ] Apply the F1 CSV-injection fix (or accept the risk in writing).
- [ ] Set `smtp.mode` to `starttls` or `ssl` (F2).
- [ ] Point `verify_ssl` at the customer CA bundle where feasible (F3).
- [ ] Create a dedicated, **read-only** Prism Central service account; confirm
      it cannot perform write operations.
- [ ] Run the process under a dedicated non-root OS service account.
- [ ] `chmod 600 .env` (or use a secret store); add `.env` to `.gitignore`.
- [ ] Restrict permissions on `out/`, `logs/`, and the config directory.
- [ ] Confirm outbound firewall allows only PC:9440 and the SMTP relay.
- [ ] Pin exact dependency versions and, for air-gapped installs, vendor the
      wheels and verify hashes (`pip install --require-hashes`).
- [ ] Re-run `bandit` and `pip-audit` in the customer's CI before each release.

---

## 6. Dependencies (SBOM) and licenses

| Package | Purpose | License |
|---|---|---|
| requests | HTTP client to Prism Central | Apache-2.0 |
| jinja2 | HTML report templating | BSD-3-Clause |
| matplotlib | Chart PNG generation | Matplotlib (BSD-style, PSF-based) |
| pyyaml | Config parsing | MIT |

All permissive licenses. Everything else used (`smtplib`, `email`, `csv`,
`logging`, `argparse`, `json`, `concurrent.futures`, `base64`) is Python
standard library. `pip-audit` reported no known vulnerabilities in the resolved
dependency tree at review time.

---

## 7. Reproduce these checks

From the project root, on any machine with the dependencies installed:

```bash
# 1. Static analysis
pip install bandit
bandit -r . -x ./tests,./.venv

# 2. Dependency CVE scan
pip install pip-audit
pip-audit -r requirements.txt

# 3. Secret / dangerous-pattern grep
grep -rniE 'eval\(|exec\(|os\.system|subprocess|shell=True|pickle|yaml\.load\(' --include='*.py' .
grep -riE 'password|secret|api[_-]?key' config.yaml .env.example   # expect placeholders only

# 4. Unit tests (behavioural correctness)
python3 -m unittest discover -s tests
```

For a deeper assessment the customer may also run their standard SAST/DAST,
software-composition-analysis, and a secrets scanner (e.g. `gitleaks`,
`trufflehog`) across the repository and its git history.

---

*Reviewed against the code as delivered. Re-review after any change to
`mailer.py`, `collector/client.py`, `render/csvout.py`, or `requirements.txt`.*
