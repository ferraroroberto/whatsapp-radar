@echo off
chcp 65001 >nul
REM ============================================================================
REM  WEBAPP - standalone FastAPI admin (plain HTTP on :8455; Tailscale/Cloudflare
REM  terminate TLS)
REM ----------------------------------------------------------------------------
REM  Daily use: launch tray.bat instead — it adopt-or-spawns the webapp for you.
REM  This bat is for headless boxes, dev iteration, or when you want the webapp
REM  without the tray icon.
REM ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [ERROR] .venv missing. Run setup.bat first.
    exit /b 1
)

cd /d "%SCRIPT_DIR%" || exit /b 1

"%VENV_PY%" -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8455

exit /b %ERRORLEVEL%
