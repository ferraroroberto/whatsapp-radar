@echo off
REM Ergonomic wrapper: `scripts\wr <command>` == `python launcher.py <command>`.
"%~dp0..\.venv\Scripts\python.exe" "%~dp0..\launcher.py" %*
