@echo off
REM Ergonomic wrapper: `scripts\wr <command>` == `python scripts/launcher.py <command>`.
"%~dp0..\.venv\Scripts\python.exe" "%~dp0launcher.py" %*
