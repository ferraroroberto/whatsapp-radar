# Run the Playwright smoke suite. By default it self-boots a disposable webapp
# on a free port (WR_E2E_AUTOBOOT=1); a live tray on :8455 is not required.
#
# Usage:
#   .\scripts\run-e2e.ps1            # run the smoke suite (auto-boot)
#   .\scripts\run-e2e.ps1 --headed   # forward any extra args to pytest

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "[X] .venv missing -- run setup.bat first." -ForegroundColor Red
    exit 1
}

$env:PYTHONUTF8 = "1"
$env:WR_E2E_AUTOBOOT = "1"
try {
    & $python -m pytest -m smoke -v tests/e2e @args
    $code = $LASTEXITCODE
}
finally {
    Remove-Item Env:\WR_E2E_AUTOBOOT -ErrorAction SilentlyContinue
}
exit $code
