"""The gate's dead-code-ratchet stage, driven end to end against the real script.

Each case copies the vendored `scripts/dead_code_ratchet.py` into a throwaway git repo
and runs the wrapper on it, so the exit-code mapping is checked against the script's
real exits: a clean tree passes, a new uncalled function fails by name, and a Python
file that does not parse is NOT CONFIRMED -- never a pass, never a skip. No network, no
scan of this tree, no sidecar, no message sent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import dead_code_ratchet_gate as gate

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
LIVE = "def helper() -> int:\n    return 1\n\n\ndef main() -> int:\n    return helper()\n"


def _git(root: Path, *args: str) -> None:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PYTHONUTF8": "1"}
    subprocess.run([GIT or "git", *args], cwd=root, env=env, check=True, capture_output=True)


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A baselined throwaway repo: returns (the copied script, its `app.py`)."""
    assert GIT, "git is a prerequisite of the dead code ratchet"
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "dead_code_ratchet.py"
    shutil.copy(gate.SCRIPT, script)
    (tmp_path / ".fleet.toml").write_text("[dead_code_ratchet]\nenabled = true\n", encoding="utf-8")
    module = tmp_path / "app.py"
    module.write_text(LIVE, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    subprocess.run(
        [sys.executable, str(script), "--init"], cwd=tmp_path, check=True, capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PYTHONUTF8": "1"},
    )
    return script, module


def test_a_clean_tree_passes(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, _module = sandbox
    assert gate.run(script) == 0
    assert capsys.readouterr().out.rstrip().splitlines()[-1].startswith("OK: dead code ratchet")


def test_a_new_uncalled_function_fails_by_file_and_name(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, module = sandbox
    module.write_text(LIVE + "\n\ndef orphaned_helper() -> None:\n    pass\n", encoding="utf-8")
    _git(script.parents[1], "add", "-A")
    assert gate.run(script) == 1
    out = capsys.readouterr().out
    assert "app.py" in out
    assert "orphaned_helper" in out
    assert out.rstrip().splitlines()[-1].startswith("FAIL: dead code ratchet")


def test_a_python_file_that_does_not_parse_is_not_confirmed(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, module = sandbox
    module.write_text("def broken(:\n", encoding="utf-8")
    _git(script.parents[1], "add", "-A")
    assert gate.run(script) == 1
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("FAIL: dead code ratchet")
    assert "NOT CONFIRMED" in last


def test_the_pre_ship_gate_runs_the_wrapper_once_after_the_doc_refs_ratchet() -> None:
    ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [ln.strip() for ln in ps1.splitlines() if ln.lstrip().startswith("& ")]
    assert stages.count("& $python -m scripts.dead_code_ratchet_gate") == 1
    assert not any("dead_code_ratchet.py" in ln for ln in stages)
    wrapper = stages.index("& $python -m scripts.dead_code_ratchet_gate")
    assert stages.index("& $python -m scripts.doc_refs_ratchet_gate") < wrapper


def test_the_ratchet_is_enabled_with_a_committed_baseline_and_a_vendored_script() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    table = fleet["dead_code_ratchet"]
    assert table["enabled"] is True
    assert (REPO_ROOT / table["baseline"]).is_file()
    assert fleet["vendored"]["dead_code_ratchet"]["dest"] == "scripts/dead_code_ratchet.py"
    assert gate.SCRIPT.is_file()
