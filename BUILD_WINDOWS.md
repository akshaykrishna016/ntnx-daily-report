# Building the Windows executable

This packages `ntnx-daily-report` into a single `ntnx-daily-report.exe` that
runs on Windows **with no Python installed** on the target machine — handy for
running it locally once a month instead of on a scheduled Linux host.

## Important: where it must be built

A Windows `.exe` has to be built **on Windows** (PyInstaller is not a
cross-compiler). Two ways to get one:

- **Option A — build on any Windows machine** (needs Python once, to build).
- **Option B — build in GitHub Actions** (no Windows machine needed).

You only need Python/PyInstaller on the *build* machine. The machine that
*runs* the exe needs nothing.

---

## Option A — build on a Windows machine

1. Install **Python 3.9+** from <https://www.python.org/downloads/> and tick
   **"Add python.exe to PATH"** during setup.
2. Copy the whole `ntnx-daily-report` folder to the Windows machine.
3. Double-click **`build_windows.bat`** (or run it from a Command Prompt in that
   folder). It creates an isolated build environment, installs the four
   dependencies plus PyInstaller, and builds the exe.
4. When it finishes you'll have:
   ```
   dist\ntnx-daily-report.exe     <- the executable
   dist\config.yaml               <- editable config beside it
   dist\assets\                   <- (if you had logo PNGs) copied here
   ```

That `dist\` folder is the whole distributable — copy it wherever you want to
run the report.

---

## Option B — build in GitHub Actions (no Windows machine)

1. Push this project to a GitHub repo (the workflow file
   `.github/workflows/build-windows.yml` is included).
2. In the repo, open **Actions → build-windows-exe → Run workflow**.
3. When it finishes, download the **ntnx-daily-report-windows** artifact — it
   contains `ntnx-daily-report.exe` and a starter `config.yaml`.

---

## Running the exe

Put `config.yaml` (and optionally `assets\siemens_logo.png` /
`assets\nutanix_logo.png`) in the **same folder** as the exe. Then:

```
ntnx-daily-report.exe                  prompts for PC + asks whether to email
ntnx-daily-report.exe --dry-run        generate to .\out, never email
ntnx-daily-report.exe --mock --dry-run offline demo with sample data
```

Because the shipped `config.yaml` leaves the Prism Central and SMTP sections
blank, a plain run **prompts** you for the PC IP/FQDN and password, then asks
whether to email the report or just write it to `.\out` — exactly the
"run it once" flow. `out\` and `logs\` are created next to the exe.

---

## Good to know

- **First run is a little slow** (a few seconds) while matplotlib builds its
  font cache; subsequent runs are fast.
- **The exe is large** (~60–120 MB) because matplotlib and numpy are bundled.
  That's expected for a self-contained Python exe.
- **SmartScreen / antivirus** may warn about an unsigned executable the first
  time. For production, code-sign the exe with your organization's certificate;
  for a quick local run, choose "More info → Run anyway".
- **What's inside vs. outside the exe:** the report template and the `--mock`
  fixtures are bundled inside (read-only); `config.yaml`, `.env`, `assets\`,
  and the `out\`/`logs\` output live next to the exe so you can edit and keep
  them.
- **The plain Python script is unchanged.** All of the exe-specific path
  handling is guarded by a "am I a frozen exe?" check, so
  `python report.py ...` behaves exactly as before — nothing about your Siemens
  testing changes.
- **Rebuild** whenever the code changes: re-run `build_windows.bat` (or the
  Actions workflow). Editing `config.yaml` does **not** require a rebuild — it's
  read from beside the exe at runtime.
