"""The gate's windows-only-ratchet stage, driven end to end against the real script.

Each case copies the vendored `scripts/windows_only_ratchet.py` into a throwaway git repo and runs
the wrapper on it, so the exit-code mapping is checked against the script's real exits: a clean
tree passes, an unmarked Windows-only test fails by file, the marker clears it, and a test module
that does not parse is NOT CONFIRMED -- never a pass, never a skip. No network, no scan of this
tree.
"""

from __future__ import annotations

import shutil
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import windows_only_ratchet_gate as gate
from tests.gates.test_runtime_write_ratchet_gate import _run, _stage

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
TABLE = "[windows_only_ratchet]\nenabled = true\n"
TEST = Path("tests") / "test_probe.py"
QUIET = "def test_quiet() -> None:\n    assert True\n"
UNMARKED = "import os\n\n\ndef test_opens() -> None:\n    os.startfile('x')\n"
MARKED = (
    "import os\n\nimport pytest\n\n\n"
    "@pytest.mark.windows_only\n"
    "def test_opens() -> None:\n    os.startfile('x')\n"
)


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A baselined throwaway repo: returns (the copied script, its test module)."""
    assert GIT, "git is a prerequisite of the windows-only ratchet"
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / gate.SCRIPT.name
    shutil.copy(gate.SCRIPT, script)
    (tmp_path / ".fleet.toml").write_text(TABLE, encoding="utf-8")
    module = tmp_path / TEST
    module.parent.mkdir(parents=True)
    module.write_text(QUIET, encoding="utf-8")
    _run(tmp_path, GIT or "git", "init", "-q")
    _stage(tmp_path)
    _run(tmp_path, sys.executable, str(script), "--init")
    return script, module


def test_a_clean_tree_passes(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, _module = sandbox
    assert gate.run(script) == 0
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("OK: windows only ratchet")


def test_an_unmarked_windows_only_test_fails_by_file(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, module = sandbox
    module.write_text(UNMARKED, encoding="utf-8")
    _stage(script.parents[1])
    assert gate.run(script) == 1
    out = capsys.readouterr().out
    assert "test_probe.py" in out
    assert out.rstrip().splitlines()[-1].startswith("FAIL: windows only ratchet")


def test_the_marker_clears_the_hit(sandbox: tuple[Path, Path]) -> None:
    script, module = sandbox
    module.write_text(MARKED, encoding="utf-8")
    _stage(script.parents[1])
    assert gate.run(script) == 0


def test_a_test_module_that_does_not_parse_is_not_confirmed(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, module = sandbox
    module.write_text("def test_broken(:\n", encoding="utf-8")
    _stage(script.parents[1])
    assert gate.run(script) == 1
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("FAIL: windows only ratchet")
    assert "NOT CONFIRMED" in last


def test_the_pre_ship_gate_runs_the_wrapper_once_after_the_request_budget_ratchet() -> None:
    ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [ln.strip() for ln in ps1.splitlines() if ln.lstrip().startswith("& ")]
    assert stages.count("& $python -m scripts.windows_only_ratchet_gate") == 1
    wrapper = stages.index("& $python -m scripts.windows_only_ratchet_gate")
    assert stages.index("& $python -m scripts.request_budget_ratchet_gate") < wrapper


def test_the_ratchet_is_enabled_with_a_committed_baseline_and_vendored() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    table = fleet["windows_only_ratchet"]
    assert table["enabled"] is True
    assert (REPO_ROOT / table["baseline"]).is_file()
    assert fleet["vendored"]["windows_only_ratchet"]["dest"] == "scripts/windows_only_ratchet.py"
    assert gate.SCRIPT.is_file()

