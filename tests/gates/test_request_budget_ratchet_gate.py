"""The gate's request-budget-ratchet stage, driven end to end against the real script.

Each case copies the vendored `scripts/request_budget_ratchet.py` and its reader into a throwaway
git repo and runs the wrapper on it, so the exit-code mapping is checked against the script's
real exits: a clean tree passes, a new fixed-rate poll fails by file, and JS that never closes
is NOT CONFIRMED -- never a pass, never a skip. No network, no scan of this tree.
"""

from __future__ import annotations

import shutil
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import request_budget_ratchet_gate as gate
from tests.gates.test_runtime_write_ratchet_gate import _run, _stage

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
TABLE = '[request_budget_ratchet]\nenabled = true\nrules = ["fixed-rate-poll"]\n'
PAGE = Path("app") / "webapp" / "static" / "app.js"
QUIET = "export const label = 'idle';\n"
POLLING = "setInterval(function () { fetch('/api/state'); }, 5000);\n"


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A baselined throwaway repo: returns (the copied script, its page)."""
    assert GIT, "git is a prerequisite of the request budget ratchet"
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / gate.SCRIPT.name
    for source in (gate.SCRIPT, gate.SCRIPT.with_name("request_budget_scan.py")):
        shutil.copy(source, script.with_name(source.name))
    (tmp_path / ".fleet.toml").write_text(TABLE, encoding="utf-8")
    page = tmp_path / PAGE
    page.parent.mkdir(parents=True)
    page.write_text(QUIET, encoding="utf-8")
    _run(tmp_path, GIT or "git", "init", "-q")
    _stage(tmp_path)
    _run(tmp_path, sys.executable, str(script), "--init")
    return script, page


def test_a_clean_tree_passes(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, _page = sandbox
    assert gate.run(script) == 0
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("OK: request budget ratchet")


def test_a_new_fixed_rate_poll_fails_by_file(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, page = sandbox
    page.write_text(QUIET + POLLING, encoding="utf-8")
    _stage(script.parents[1])
    assert gate.run(script) == 1
    out = capsys.readouterr().out
    assert "app.js" in out
    assert out.rstrip().splitlines()[-1].startswith("FAIL: request budget ratchet")


def test_js_that_never_closes_is_not_confirmed(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, page = sandbox
    (page.parent / "broken.js").write_text("const s = 'never closed;\n", encoding="utf-8")
    _stage(script.parents[1])
    assert gate.run(script) == 1
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("FAIL: request budget ratchet")
    assert "NOT CONFIRMED" in last


def test_the_pre_ship_gate_runs_the_wrapper_once_after_the_ux_surface_ratchet() -> None:
    ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [ln.strip() for ln in ps1.splitlines() if ln.lstrip().startswith("& ")]
    assert stages.count("& $python -m scripts.request_budget_ratchet_gate") == 1
    wrapper = stages.index("& $python -m scripts.request_budget_ratchet_gate")
    assert stages.index("& $python -m scripts.ux_surface_ratchet_gate") < wrapper


def test_the_ratchet_is_enabled_with_a_committed_baseline_and_both_components_vendored() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    table = fleet["request_budget_ratchet"]
    assert table["enabled"] is True
    assert (REPO_ROOT / table["baseline"]).is_file()
    vendored = fleet["vendored"]
    assert vendored["request_budget_ratchet"]["dest"] == "scripts/request_budget_ratchet.py"
    assert vendored["request_budget_scan"]["dest"] == "scripts/request_budget_scan.py"
    assert gate.SCRIPT.is_file()
