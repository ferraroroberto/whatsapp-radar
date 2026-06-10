@echo off
chcp 65001 >nul
REM ============================================================================
REM  WHATSAPP RADAR TRAY - tray icon that owns the admin webapp lifecycle
REM ----------------------------------------------------------------------------
REM  Launch on login (Startup folder) for an always-on phone-first admin.
REM
REM  Idempotent:
REM    tray.bat              -> no-op if a WhatsApp Radar tray is already running
REM    tray.bat --restart    -> stop the running tray (and its webapp on :8455 +
REM                             cloudflared) and start a fresh one
REM
REM  Detection matches the tray process by command line + this project's .venv
REM  path via CIM, then kills BY PID with /T. We never blanket-kill pythonw, so
REM  sister-app trays (AppLauncher, PhotoOCR, local-llm-hub, ...) are untouched.
REM
REM  --restart is orphan-proof: besides killing the tray subtree, it reclaims
REM  this app's webapp port :8455 by its owning PID, regardless of parentage,
REM  scoped to processes under THIS repo's .venv. See project-scaffolding#29.
REM ============================================================================

setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv\Scripts"
set "VENV_PYW=%VENV_DIR%\pythonw.exe"
set "VENV_PY=%VENV_DIR%\python.exe"

cd /d "%SCRIPT_DIR%" || exit /b 1

set "WANT_RESTART="
if /i "%~1"=="--restart" set "WANT_RESTART=1"
if /i "%~1"=="-r"        set "WANT_RESTART=1"

set "PS=C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
set "TRAY_VENV=%SCRIPT_DIR%.venv"
set "TRAY_PS=%SCRIPT_DIR%app\tray\tray_lifecycle.ps1"
if not exist "%TRAY_PS%" (
    echo ERROR: missing tray helper "%TRAY_PS%" -- vendor app\tray\tray_lifecycle.ps1 from the scaffold.
    exit /b 1
)
set "TRAY_PIDS="
for /f "usebackq delims=" %%P in (`%PS% -NoProfile -NonInteractive -File "%TRAY_PS%" detect -VenvDir "%TRAY_VENV%" -TrayMatch "launcher\.py\s+tray"`) do (
    if defined TRAY_PIDS (set "TRAY_PIDS=!TRAY_PIDS! %%P") else (set "TRAY_PIDS=%%P")
)

if defined TRAY_PIDS if not defined WANT_RESTART (
    echo WhatsApp Radar tray is already running ^(PID: !TRAY_PIDS!^).
    echo Run "tray.bat --restart" to stop it and start fresh.
    exit /b 0
)

if defined WANT_RESTART (
    if defined TRAY_PIDS (
        echo Stopping previous WhatsApp Radar tray ^(PID: !TRAY_PIDS!^)...
        for %%P in (!TRAY_PIDS!) do (
            taskkill /T /F /PID %%P >nul 2>&1
        )
    )
    REM Orphan-proof: reclaim this app's webapp port from ANY holder whose
    REM command line is under this repo's .venv, even one detached from the tray
    REM subtree above. Matching on CommandLine (not the image path) keeps the
    REM sweep scoped to THIS repo's children only.
    set "RECLAIM_VENV=%SCRIPT_DIR%.venv"
    %PS% -NoProfile -NonInteractive -File "%TRAY_PS%" reclaim -VenvDir "%RECLAIM_VENV%" -Ports "8455"
    REM Give Windows a moment to release :8455 before rebinding.
    ping 127.0.0.1 -n 3 >nul
)

REM Prefer pythonw.exe so no console window stays open.
if exist "%VENV_PYW%" (
    start "WhatsApp Radar Tray" "%VENV_PYW%" launcher.py tray
) else if exist "%VENV_PY%" (
    start "WhatsApp Radar Tray" "%VENV_PY%" launcher.py tray
) else (
    start "WhatsApp Radar Tray" pythonw launcher.py tray
)
exit /b 0
