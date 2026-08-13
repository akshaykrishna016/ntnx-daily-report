# ntnx-daily-report

Daily Nutanix resource, capacity and efficiency report for Siemens Limited.

The script pulls inventory and stats from Prism Central over the REST API,
renders a branded executive + deep-dive report, and emails it to a distribution
list every morning. Delivery model **v1.1**: the email **body** is a compact,
image-free summary; the **full report** is a self-contained HTML **attachment**
(all charts and logos embedded as base64, zero external references); the **VM
inventory** is a second attachment (CSV).

It is built for an ops engineer to maintain: readable, one-statement-per-line
Python, a docstring on every module and function, and only four third-party
dependencies (`requests`, `jinja2`, `matplotlib`, `pyyaml`).

---

## Repository layout

```
ntnx-daily-report/
├── report.py               # entry point (CLI, orchestration)
├── probe_metrics.py        # one-off: dump live stats field names (run first)
├── units.py                # pure unit-conversion / RAG / aggregation helpers
├── config.yaml             # all settings (no secrets)
├── .env.example            # PC_PASSWORD=..., SMTP_PASSWORD=... (secrets)
├── requirements.txt
├── collector/
│   ├── client.py           # PrismClient transport + MockClient (fixtures)
│   ├── metric_names.py     # ONE place to adjust API metric field names
│   ├── model.py            # Cluster / Host / VM / Summary dataclasses
│   ├── clusters.py         # cluster inventory + stats (+ PC-cluster exclusion)
│   ├── hosts.py            # host inventory + stats
│   ├── vms.py              # VM inventory + stats + guest storage + efficiency
│   ├── efficiency.py       # v3 groups: efficiency, guest storage, alerts, runway
│   ├── parsing.py          # stats / groups response parsing helpers
│   └── summarize.py        # estate-wide roll-up for KPIs and gauges
├── render/
│   ├── charts.py           # matplotlib PNG charts (base64 data URIs)
│   ├── html.py             # report + email-body rendering
│   ├── csvout.py           # full VM inventory CSV writer
│   └── templates/
│       └── report.html.j2  # self-contained report template (from mockup v3)
├── mailer.py               # SMTP send (MIME multipart/mixed, §9)
├── assets/                 # drop siemens_logo.png / nutanix_logo.png here
├── fixtures/
│   └── sample_data.json    # canned API responses for --mock
├── tests/
│   └── test_transforms.py  # unit tests (§12)
├── out/                    # --dry-run output (report, body, CSV)
└── logs/                   # rotating report.log (14 days)
```

---

## Install

Python 3.9+ on a Linux management host (RHEL/Ubuntu, no GUI needed).

```bash
cd /opt/ntnx-daily-report
python3 -m pip install -r requirements.txt
```

Matplotlib runs headless (Agg backend) — no display or X server is required.

---

## Configuration

All non-secret settings live in `config.yaml`. The important ones:

| Key | Meaning |
|---|---|
| `prism_central.host` / `port` | Prism Central address (port 9440). |
| `prism_central.username` | Read-only service account. |
| `prism_central.verify_ssl` | `true`, `false`, or a path to a CA bundle. PC usually has a self-signed cert, so `false` is typical. |
| `report.window_hours` | Reporting window (default 24). |
| `report.thresholds.amber` / `red` | RAG bands. Green `< amber`; amber `amber–red` inclusive; red `> red`. |
| `report.top_n_vms` | Bars per Top Talkers chart. |
| `report.vm_table_rows` | VM rows shown inline (full list always in the CSV). |
| `report.exclude_vm_patterns` | Glob patterns for VM names to exclude (e.g. `NTNX-*-CVM`). |
| `smtp.mode` | `plain` (25) / `starttls` (587) / `ssl` (465). |
| `smtp.username` | `null` → anonymous relay (no login attempted). |
| `smtp.recipients` | The PDL. |
| `branding.assets_dir` | Where the logo PNGs live. |

### Secrets (never in config or logs)

Secrets come from environment variables, optionally loaded from a `.env` file in
the project directory (parsed manually — no extra dependency):

```bash
cp .env.example .env
# then edit .env:
#   PC_PASSWORD=<service account password>
#   SMTP_PASSWORD=<only if smtp.username is set>
```

A real exported environment variable always wins over the `.env` file.

---

## First live run (do this once)

1. **Probe the metric names.** Field names vary by PC / AOS version, so confirm
   them before scheduling:

   ```bash
   PC_PASSWORD='...' python3 probe_metrics.py
   ```

   It prints each cluster's function list (confirm which entity is the Prism
   Central self-cluster) and the metric keys actually returned by the cluster,
   host and VM stats endpoints. If any differ from
   `collector/metric_names.py`, edit that one file — nothing else.

2. **Send a test report to yourself:**

   ```bash
   PC_PASSWORD='...' python3 report.py --to you@siemens.com
   ```

   Confirm the summary body renders in Outlook (no overflow, no image
   placeholders), the attached HTML opens in a browser with all charts visible,
   and the CSV opens in Excel.

---

## CLI

```bash
python report.py                     # collect → render → email (the PDL)
python report.py --dry-run           # collect + render; write ./out; do NOT email
python report.py --mock              # use fixtures/sample_data.json (offline)
python report.py --config /path.yaml # alternate config
python report.py --to a@b.com        # override recipients (repeatable; testing)
```

`--mock --dry-run` together run **fully offline** — no Prism Central and no
mail relay. This is how the layout is developed and reviewed:

```bash
python report.py --mock --dry-run
# writes:
#   out/report_YYYYMMDD.html         (open in a browser — self-contained)
#   out/email_body.html              (the compact summary body)
#   out/vm_inventory_YYYYMMDD.csv    (every VM)
```

---

## Two ways to run

**1. Ad-hoc / run-it-once (no cron, no setup).** Leave `prism_central.host`
(and the `smtp` block) **blank** in `config.yaml`. Just run:

```bash
python report.py
```

The script prompts for what it needs and asks how you want the output:

```
Prism Central connection
  Prism Central IP / FQDN: 10.20.30.40
  Username [svc_report_ro]:
  Prism Central password:            # hidden, never stored or logged
...
Send the report by email now? [y/N]: n
Report generated in ./out (not emailed). Open out/report_20260812.html to view it.
```

Answer **n** and it just writes the report/CSV to `./out` for you to open or
forward. Answer **y** and it asks for the SMTP relay, from address and
recipients, then sends. Nothing is written to disk as a secret; the passwords
live only in memory for that run. This is the mode for "I run it locally once a
month."

**2. Scheduled / unattended (cron).** Fill in `prism_central` and `smtp` in
`config.yaml`, provide `PC_PASSWORD` (and `SMTP_PASSWORD` if needed) via env /
`.env`, and the script runs with **no prompts** and emails the report. Env vars
always win over prompts, and if a terminal isn't attached the script fails fast
with a clear message instead of hanging. See "Scheduling (cron)" below.

Flags still apply in both modes: `--dry-run` always skips email, `--to` overrides
recipients, `--mock` uses the offline fixtures.

---

## Scheduling (cron)

Runs once per run, stateless. Example — 08:05 IST daily:

```cron
5 8 * * * cd /opt/ntnx-daily-report && /usr/bin/python3 report.py >> logs/cron.log 2>&1
```

The process exits non-zero if generation fails, so cron/monitoring can alert on
it. Even on failure a notification email is sent to the PDL (see below).

---

## Testing

```bash
python3 -m unittest discover -s tests      # or: python3 -m pytest tests/
```

Covers ppm→%, bytes→GiB/TiB, RAG banding incl. the 70 and 85 boundaries,
avg/max aggregation, efficiency-status normalization, and CSV row generation
(design document, §12).

---

## Error handling — the report still goes out

Partial failures degrade a cell, not the whole run (design document, §11):

- **A single VM's stats call fails** → that row shows "—", a warning is logged,
  and the footnote counts the failures.
- **Efficiency / groups call fails** → the tiles show "N/A" with an
  insufficient-baseline footnote.
- **A cluster or host stats call fails** → that entity's usage shows zeros and a
  warning is logged; other entities are unaffected.
- **A VM has no NGT** → guest Used/Free show "—"; hypervisor-allocated Storage
  is still reported.
- **Cluster inventory fails entirely** → the only hard stop. The script emails a
  short failure notice to the same PDL (so silence never masquerades as success)
  and exits 1.

The Prism Central self-cluster is excluded immediately after inventory (it
rejects stats calls with HTTP 400 CLU-10008), so it never reaches a stats call.

Logs go to stdout (INFO) and `logs/report.log` (DEBUG, 14-day rotation).
Passwords and `Authorization` headers are never logged.

---

## Delivery model (v1.1) — MIME structure

```
multipart/mixed
├── multipart/alternative
│   ├── text/plain            (summary)
│   └── text/html             (summary body — no images)
├── text/html attachment      report_YYYYMMDD.html   (self-contained)
└── text/csv attachment       vm_inventory_YYYYMMDD.csv
```

Subject line:
`[Nutanix Daily Report] Siemens Ltd — YYYY-MM-DD — N clusters, M VMs[, ⚠ K critical alerts]`

---

## Deviations from the design document (with justification)

The spec was followed as written; the differences below are additive
decompositions or explicit choices among options the spec offered. None change
the required behaviour, layout, or delivery model.

1. **Extra modules beyond the §3 tree** — `collector/model.py`,
   `collector/parsing.py`, `collector/summarize.py`, `render/csvout.py`, and a
   top-level `units.py`. §3 lists the core files; these split the data model,
   response-parsing, roll-up and CSV logic into their own small, docstringed
   files for readability. `units.py` is referenced directly by §5.4 and §13.

2. **Email body built in Python, not a second template** — §3 lists only
   `report.html.j2`. The compact body (§7b) needs *every* style inlined for
   Outlook, which is clearer to build as small string helpers in
   `render/html.py` than in a Jinja template. The self-contained report still
   uses the `report.html.j2` template as specified.

3. **Guest storage via the v3 groups API** (`guest.disk_*` attributes) rather
   than `v3 vms/list`. §5.3 explicitly permits "or groups API attributes
   (`guest.disk_usage` family)"; using groups keeps VM collection to one extra
   estate-wide call instead of N per-VM calls.

4. **Critical alerts via the v3 groups `alert` entity** rather than the v4
   `/monitoring/v4.0/alerts` endpoint. §5.5 permits either. The live severity
   filter string (`kCritical`) may differ by version and is easy to adjust in
   `collector/efficiency.py`.

5. **`out/email_body.html` is written on `--dry-run`** in addition to the report
   and CSV. §4 mentions `report.html`/`report.csv`; the acceptance checklist
   (§12.1) asks for `report_{date}.html`, `email_body.html` and
   `vm_inventory_{date}.csv`, which is what is produced.

6. **Fixture stats shape** is modelled as `{metric: [{timestamp, value}, ...]}`
   inside the v4 `data[0]` envelope — representative of the v4 time-series
   response. The live `PrismClient` and the `MockClient` both feed the same
   `parsing.extract_samples` path, and `probe_metrics.py` + `metric_names.py`
   exist precisely to reconcile the real field names on first run.

7. **Sample estate size** — the fixture has 3 clusters, 10 hosts and 52 in-scope
   VMs (plus CVMs and the PC VM that get excluded, one VM with failing stats,
   and 10 VMs without NGT), matching the "~50 VMs" build requirement rather than
   the "~30" figure in §12.
