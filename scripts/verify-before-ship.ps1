# Pre-ship verification gate.
#
# Runs the full validation pipeline locally before a change is declared "done":
# byte-compile, ruff, the static, docs, comment and structure ratchets, mypy (strict), the offline
# pytest suite, then the Playwright e2e suite (Chromium + WebKit/iPhone) against a
# disposable webapp the script boots itself on a free port. The e2e stage is routed by
# scripts/classify_e2e.py against the [e2e] table in .fleet.toml: nothing for a
# diff with no browser surface, the smoke target for inert assets, a surface
# slice or the full suite otherwise. An unclassified path, an empty diff or a
# router failure runs the full suite and prints why.
#
# Usage:
#   powershell -File scripts\verify-before-ship.ps1
#
# -Classifier  router script to ask (default scripts\classify_e2e.py); a seam so
#              the fail-safe paths can be exercised with a stub.
# -RouteOnly   print the e2e routing decision and exit, running no stage.
#
# Exits non-zero on the first failure with the offending output left visible.

param(
    [string]$Classifier = "scripts\classify_e2e.py",
    [switch]$RouteOnly
)

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

# Ask the router what the diff needs. Every way of not getting a usable answer
# (non-zero exit, no E2E_TIER line, an unknown tier, no target) resolves to the
# full suite with the reason printed -- uncertainty never narrows what runs.
# An empty browser list means "the suite default", which here is both projections.
function Get-E2ERoute {
    $route = @{ Tier = "full"; Target = "tests/e2e"; Browsers = "chromium,webkit"; Reason = "" }
    try {
        $out = & $python $Classifier
        $code = $LASTEXITCODE
    }
    catch {
        $route.Reason = "router could not run ($($_.Exception.Message)) -- running the full suite (fail-safe)"
        return $route
    }
    if ($code -ne 0) {
        $route.Reason = "router failed (exit $code) -- running the full suite (fail-safe)"
        return $route
    }
    $kv = @{}
    foreach ($line in $out) {
        if ($line -match '^(E2E_[A-Z_]+)=(.*)$') { $kv[$matches[1]] = $matches[2] }
    }
    $tier = $kv["E2E_TIER"]
    if (-not $tier) {
        $route.Reason = "router gave no E2E_TIER verdict -- running the full suite (fail-safe)"
    } elseif ($tier -notin @("skip", "static", "surface", "full")) {
        $route.Reason = "router gave unknown tier '$tier' -- running the full suite (fail-safe)"
    } elseif ($tier -ne "skip" -and -not $kv["E2E_PYTEST_TARGET"]) {
        $route.Reason = "router gave tier '$tier' without a pytest target -- running the full suite (fail-safe)"
    } else {
        $route.Tier = $tier
        $route.Target = $kv["E2E_PYTEST_TARGET"]
        if ($kv["E2E_BROWSERS"]) { $route.Browsers = $kv["E2E_BROWSERS"] }
        $route.Reason = $kv["E2E_REASON"]
    }
    return $route
}

$env:PYTHONUTF8 = "1"
Push-Location $repoRoot
try {
    $e2e = Get-E2ERoute
    if ($RouteOnly) {
        Write-Host "E2E_ROUTE tier=$($e2e.Tier) target=$($e2e.Target) browsers=$($e2e.Browsers)"
        Write-Host "E2E_ROUTE reason=$($e2e.Reason)"
        exit 0
    }

    Write-Host "==> py_compile (app, src, tests, scripts)..." -ForegroundColor Cyan
    & $python -m compileall -q app src tests scripts
    if ($LASTEXITCODE -ne 0) { Fail "byte-compile failed." }

    Write-Host "==> ruff..." -ForegroundColor Cyan
    & $python -m ruff check .
    if ($LASTEXITCODE -ne 0) { Fail "ruff failed." }

    # Whole tree, never path-routed. Calls the wrapper, not the script: the wrapper
    # fails the script's exit 3 (a detector could not run) as NOT CONFIRMED, never
    # a pass or a skip (tests/test_static_ratchet_gate.py pins it).
    Write-Host "==> static ratchet (new duplicate block / unused import or variable)..." -ForegroundColor Cyan
    & $python -m scripts.static_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "static ratchet failed or was NOT CONFIRMED." }

    # Whole tree too. Same wrapper contract: the docs ratchet's exit 3 (could not
    # scan) fails as NOT CONFIRMED (tests/test_docs_ratchet_gate.py pins it).
    Write-Host "==> docs ratchet (README size / new history marker in README or docs)..." -ForegroundColor Cyan
    & $python -m scripts.docs_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "docs ratchet failed or was NOT CONFIRMED." }

    # Whole tree too. The comment ratchet's exit 3 (a file it cannot scan) fails as
    # NOT CONFIRMED (tests/test_comment_ratchet_gate.py pins it).
    Write-Host "==> comment ratchet (no new issue reference in a code comment)..." -ForegroundColor Cyan
    & $python -m scripts.comment_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "comment ratchet failed or was NOT CONFIRMED." }

    # Whole tree too: a directory count needs every file. The structure ratchet's exit 3
    # (git unusable, an unparseable file, an unknown shape) fails as NOT CONFIRMED
    # (tests/test_structure_ratchet_gate.py pins it).
    Write-Host "==> structure ratchet (file / function / directory size, src -> app imports, root layout)..." -ForegroundColor Cyan
    & $python -m scripts.structure_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "structure ratchet failed or was NOT CONFIRMED." }

    Write-Host "==> mypy (strict, src + app)..." -ForegroundColor Cyan
    & $python -m mypy src app
    if ($LASTEXITCODE -ne 0) { Fail "mypy failed." }

    Write-Host "==> pytest (offline unit suite)..." -ForegroundColor Cyan
    & $python -m pytest
    if ($LASTEXITCODE -ne 0) { Fail "unit pytest suite failed." }

    if ($e2e.Tier -eq "skip") {
        Write-Host "==> e2e routing: SKIP browser suite (no e2e surface touched)" -ForegroundColor Cyan
        Write-Host "    reason: $($e2e.Reason)"
        $e2eExit = 0
    }
    else {
        Write-Host "==> e2e routing: $($e2e.Tier)" -ForegroundColor Cyan
        Write-Host "    reason: $($e2e.Reason)"
        # A surface slice names several targets space-separated; pytest needs
        # each as its own argument.
        $e2eArgs = @($e2e.Target -split '\s+' | Where-Object { $_ }) + @("-q")
        foreach ($b in ($e2e.Browsers -split ',' | Where-Object { $_ })) {
            $e2eArgs += @("--browser", $b)
        }
        Write-Host "==> pytest e2e ($($e2e.Tier): $($e2e.Target), $($e2e.Browsers); auto-booted)..." -ForegroundColor Cyan
        $env:WR_E2E_AUTOBOOT = "1"
        try {
            & $python -m pytest @e2eArgs
            $e2eExit = $LASTEXITCODE
        }
        finally {
            Remove-Item Env:\WR_E2E_AUTOBOOT -ErrorAction SilentlyContinue
        }
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
