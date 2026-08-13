# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build spec for ntnx-daily-report (Windows single-file .exe).
#
# Build (on a Windows machine with Python 3.9+):
#     pip install -r requirements.txt pyinstaller
#     pyinstaller --clean --noconfirm ntnx-daily-report.spec
#
# Produces: dist\ntnx-daily-report.exe
#
# What is bundled INSIDE the exe (read-only, unpacked to a temp dir at runtime):
#   * render/templates/report.html.j2   -- the report template
#   * fixtures/sample_data.json          -- the --mock offline fixtures
#
# What stays OUTSIDE, next to the exe (operator-editable / output):
#   * config.yaml, .env, assets\*.png    -- you place these next to the exe
#   * out\, logs\                        -- created next to the exe at runtime
#
# report.py detects the frozen exe (sys.frozen) and resolves those two groups of
# paths accordingly; running the plain .py is unaffected.

block_cipher = None

a = Analysis(
    ['report.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('render/templates/report.html.j2', 'render/templates'),
        ('fixtures/sample_data.json', 'fixtures'),
    ],
    # matplotlib's Agg backend is imported via a string in charts.py, so it is
    # declared here to be safe; PyInstaller's matplotlib hook handles mpl-data.
    hiddenimports=['matplotlib.backends.backend_agg'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim GUI backends we never use to keep the exe smaller.
        'tkinter',
        'PyQt5',
        'PySide2',
        'PIL.ImageQt',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ntnx-daily-report',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Console app: the interactive prompts (PC IP, password, email choice) and
    # the log output need a terminal window.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
