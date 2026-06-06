@echo off
REM Ergonomic wrapper: `wr <command>` == `python launcher.py <command>`.
"%~dp0.venv\Scripts\python.exe" "%~dp0launcher.py" %*
