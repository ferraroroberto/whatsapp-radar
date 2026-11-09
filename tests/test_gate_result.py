"""The pre-ship gate ends every run with a `GATE-RESULT: PASS|FAIL` stdout line.

A pipeline reports its last command's status, so `verify-before-ship.ps1 | tail`
hands the caller `tail`'s 0 and a red gate reads green. The verdict line is the
part a pipe cannot rewrite. The helper block in the gate (between the
`gate-verdict` markers) is run for real, over a captured stdout (the same
anonymous pipe `|` creates), for a pass, a failed stage and an early terminating
error; the real script is run for the path that needs no stage to execute.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "verify-before-ship.ps1"
POWERSHELL = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not POWERSHELL.is_file(),
    reason="the gate is a Windows PowerShell script",
)


def _gate_text() -> str:
    return GATE.read_text(encoding="utf-8").replace("\r\n", "\n")


def _verdict_block() -> str:
    match = re.search(
        r"# gate-verdict:begin\n(.*?)# gate-verdict:end", _gate_text(), re.DOTALL
    )
    assert match, "the gate lost its gate-verdict block"
    return match.group(1)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(POWERSHELL), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", *args,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=120,
        check=False,
    )


def _run_scenario(scenario: str) -> subprocess.CompletedProcess[str]:
    script = (
        '$ErrorActionPreference = "Stop"\n'
        f"{_verdict_block()}\n"
        'function Fail($message) { Write-Host "[X] $message"; Complete-Gate 1 }\n'
        f"{scenario}\n"
    )
    return _run(["-Command", script])


def _last_line(run: subprocess.CompletedProcess[str]) -> str:
    lines = [ln.rstrip() for ln in run.stdout.splitlines() if ln.strip()]
    assert lines, f"nothing on stdout; stderr={run.stderr!r}"
    return lines[-1]


def test_pass_ends_with_pass_and_exits_zero() -> None:
    run = _run_scenario("Complete-Gate 0")
    assert run.returncode == 0, run.stdout
    assert _last_line(run) == "GATE-RESULT: PASS"


def test_failed_stage_ends_with_fail_and_exits_one() -> None:
    run = _run_scenario(
        "cmd /c exit 7\n"
        'if ($LASTEXITCODE -ne 0) { Fail "stage failed." }\n'
        "Complete-Gate 0"
    )
    assert run.returncode == 1, run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"
    assert "[X] stage failed." in run.stdout


def test_early_terminating_error_reports_on_stdout_then_fails() -> None:
    run = _run_scenario('throw "boom before any stage"')
    assert run.returncode == 1, run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"
    assert "boom before any stage" in run.stdout, (
        "the error must reach stdout, not only stderr"
    )


def test_missing_venv_ends_with_fail_and_exits_one(tmp_path: Path) -> None:
    """The real script, copied beside no .venv: the first guard fails the run."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    gate = scripts / GATE.name
    shutil.copy2(GATE, gate)
    run = _run(["-File", str(gate)])
    assert run.returncode == 1, run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"
    assert ".venv missing" in run.stdout


def test_every_exit_goes_through_the_verdict() -> None:
    """A new `exit` that skips `Complete-Gate` would end a run with no verdict.

    `-RouteOnly` is the one deliberate bare exit: it runs no stage, so it
    prints no verdict a caller could mistake for a pass.
    """
    outside = _gate_text().replace(_verdict_block(), "")
    bare = re.findall(r"^\s*exit\s+\d.*$", outside, re.MULTILINE)
    assert bare == ["        exit 0"], f"exit through Complete-Gate, not directly: {bare}"
    assert _gate_text().rstrip().endswith("Complete-Gate 0"), (
        "the pass path must end on its verdict"
    )


def test_verdict_block_is_ascii_for_windows_powershell_5_1() -> None:
    """Windows PowerShell 5.1 reads a BOM-less file as ANSI, so the block stays ASCII."""
    _verdict_block().encode("ascii")
