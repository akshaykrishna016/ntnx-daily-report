@echo off
REM ---------------------------------------------------------------------------
REM Build the Windows single-file executable for ntnx-daily-report.
REM Run this on a Windows machine that has Python 3.9+ installed.
REM Double-click it, or run "build_windows.bat" from a Command Prompt.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

echo === Creating an isolated build environment (.venv) ===
python -m venv .venv || goto :error
call ".venv\Scripts\activate.bat" || goto :error

echo === Installing dependencies + PyInstaller ===
python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt pyinstaller || goto :error

echo === Building the executable ===
pyinstaller --clean --noconfirm ntnx-daily-report.spec || goto :error

echo === Staging config + assets next to the exe ===
copy /Y config.yaml "dist\config.yaml" >nul
if exist assets (
    if not exist "dist\assets" mkdir "dist\assets"
    copy /Y "assets\*.png" "dist\assets\" >nul 2>nul
)

echo.
echo ============================================================
echo  Build complete.
echo  Executable : dist\ntnx-daily-report.exe
echo  Alongside  : dist\config.yaml  (edit or leave blank to be prompted)
echo               dist\assets\      (drop siemens_logo.png / nutanix_logo.png)
echo.
echo  Run it:   cd dist
echo            ntnx-daily-report.exe            (prompts, then generates/sends)
echo            ntnx-daily-report.exe --dry-run  (never emails)
echo            ntnx-daily-report.exe --mock --dry-run   (offline demo)
echo ============================================================
goto :end

:error
echo.
echo *** Build FAILED. See the messages above. ***
exit /b 1

:end
endlocal
