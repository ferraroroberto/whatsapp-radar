@echo off
chcp 65001 >nul
REM ============================================================================
REM  WEBAPP - standalone FastAPI admin (HTTPS when a cert is present)
REM ----------------------------------------------------------------------------
REM  Daily use: launch tray.bat instead — it adopt-or-spawns the webapp for you.
REM  This bat is for headless boxes, dev iteration, or when you want the webapp
REM  without the tray icon.
REM ============================================================================

setlocal
for %%I in ("%~dp0..") do set "SCRIPT_DIR=%%~fI\"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [ERROR] .venv missing. Run setup.bat first.
    exit /b 1
)

cd /d "%SCRIPT_DIR%" || exit /b 1

REM Host, port, server and TLS come from config\webapp_config.json (and
REM WR_WEBAPP_PORT) through WebappManager, the same source the tray reads.
REM It also auto-renews a Tailscale cert expiring within 30 days before binding.
"%VENV_PY%" -m app.webapp.standalone

exit /b %ERRORLEVEL%
