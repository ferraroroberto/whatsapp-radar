# Pre-ship verification gate.
#
# Runs the full validation pipeline locally before a change is declared "done":
# byte-compile, ruff, mypy (strict), the offline pytest suite, then the
# Playwright e2e smoke suite (Chromium + WebKit/iPhone) against a disposable
# webapp the script boots itself on a free port.
#
# Usage:
#   powershell -File scripts\verify-before-ship.ps1
#
# Exits non-zero on the first failure with the offending output left visible.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$sw = [System.Diagnostics.Stopwatch]::StartNew()

function Fail($message) {
    Write-Host ""
    Write-Host "[X] $message" -ForegroundColor Red
    Write-Host ("Failed after {0:n1}s." -f $sw.Elapsed.TotalSeconds) -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $python)) {
    Fail ".venv missing -- run setup.bat first."
}

$env:PYTHONUTF8 = "1"
Push-Location $repoRoot
try {
    Write-Host "==> py_compile (app, src, tests, scripts)..." -ForegroundColor Cyan
    & $python -m compileall -q app src tests scripts
    if ($LASTEXITCODE -ne 0) { Fail "byte-compile failed." }

    Write-Host "==> ruff..." -ForegroundColor Cyan
    & $python -m ruff check .
    if ($LASTEXITCODE -ne 0) { Fail "ruff failed." }

    Write-Host "==> mypy (strict, src + app)..." -ForegroundColor Cyan
    & $python -m mypy src app
    if ($LASTEXITCODE -ne 0) { Fail "mypy failed." }

    Write-Host "==> pytest (offline unit suite)..." -ForegroundColor Cyan
    & $python -m pytest
    if ($LASTEXITCODE -ne 0) { Fail "unit pytest suite failed." }

    Write-Host "==> pytest e2e (Chromium + WebKit/iPhone, auto-booted)..." -ForegroundColor Cyan
    $env:WR_E2E_AUTOBOOT = "1"
    try {
        & $python -m pytest tests/e2e -q --browser chromium --browser webkit
        $e2eExit = $LASTEXITCODE
    }
    finally {
        Remove-Item Env:\WR_E2E_AUTOBOOT -ErrorAction SilentlyContinue
    }
    if ($e2eExit -ne 0) { Fail "Playwright e2e suite failed." }
}
finally {
    Pop-Location
}

$sw.Stop()
Write-Host ""
Write-Host ("[OK] Ready to ship -- all checks passed in {0:n1}s." -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green
exit 0
