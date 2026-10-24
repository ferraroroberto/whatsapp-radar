"""The gate's conventions-ratchet stage, driven end to end against the real script.

Each case copies the vendored `scripts/conventions_ratchet.py` into a throwaway git repo
and runs the wrapper on it, so the exit-code mapping is checked against the script's
real exits: a clean tree passes, a new violation fails by rule name, and a Python file
that does not parse is NOT CONFIRMED -- never a pass, never a skip. No network, no scan
of this tree, no sidecar, no message sent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import conventions_ratchet_gate as gate
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
CLEAN = (
    "import logging\n\nlog = logging.getLogger(__name__)\n\n\n"
    "def work() -> None:\n    log.info('ok')\n"
)
RULES = '["no-window", "logging-not-print", "no-drive-path"]'


def _env() -> dict[str, str]:
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PYTHONUTF8": "1"}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [GIT or "git", *args], cwd=root, env=_env(), check=True, capture_output=True,
        creationflags=NO_WINDOW,
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A baselined throwaway repo: returns (the copied script, its `src/mod.py`)."""
    assert GIT, "git is a prerequisite of the conventions ratchet"
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src").mkdir()
    script = tmp_path / "scripts" / "conventions_ratchet.py"
    shutil.copy(gate.SCRIPT, script)
    (tmp_path / ".fleet.toml").write_text(
        f"[conventions_ratchet]\nenabled = true\nrules = {RULES}\n", encoding="utf-8"
    )
    module = tmp_path / "src" / "mod.py"
    module.write_text(CLEAN, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    subprocess.run(
        [sys.executable, str(script), "--init"], cwd=tmp_path, check=True, capture_output=True,
        env=_env(), creationflags=NO_WINDOW,
    )
    return script, module


def test_a_clean_tree_passes(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, _module = sandbox
    assert gate.run(script) == 0
    assert capsys.readouterr().out.rstrip().splitlines()[-1].startswith("OK: conventions ratchet")


def test_a_new_print_in_library_code_fails_by_rule_name(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, module = sandbox
    module.write_text(CLEAN + "\n\ndef noisy() -> None:\n    print('hi')\n", encoding="utf-8")
    _git(script.parents[1], "add", "-A")
    assert gate.run(script) == 1
    out = capsys.readouterr().out
    assert "[logging-not-print]" in out
    assert "mod.py" in out
    assert out.rstrip().splitlines()[-1].startswith("FAIL: conventions ratchet")


def test_a_python_file_that_does_not_parse_is_not_confirmed(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, module = sandbox
    module.write_text("def broken(:\n", encoding="utf-8")
    _git(script.parents[1], "add", "-A")
    assert gate.run(script) == 1
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("FAIL: conventions ratchet")
    assert "NOT CONFIRMED" in last


def test_the_pre_ship_gate_runs_the_wrapper_once_after_the_dead_code_ratchet() -> None:
    ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [ln.strip() for ln in ps1.splitlines() if ln.lstrip().startswith("& ")]
    assert stages.count("& $python -m scripts.conventions_ratchet_gate") == 1
    assert not any("conventions_ratchet.py" in ln for ln in stages)
    wrapper = stages.index("& $python -m scripts.conventions_ratchet_gate")
    assert stages.index("& $python -m scripts.dead_code_ratchet_gate") < wrapper


def test_the_ratchet_is_enabled_with_a_committed_baseline_and_a_vendored_script() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    table = fleet["conventions_ratchet"]
    assert table["enabled"] is True
    assert (REPO_ROOT / table["baseline"]).is_file()
    assert fleet["vendored"]["conventions_ratchet"]["dest"] == "scripts/conventions_ratchet.py"
    assert gate.SCRIPT.is_file()
