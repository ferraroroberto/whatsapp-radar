"""The gate's structure-ratchet stage, driven end to end against the real script.

Each case copies the vendored `scripts/structure_ratchet.py` into a throwaway git repo
and runs the wrapper on it, so the exit-code mapping is checked against the script's
real exits: a clean tree passes, a baselined file that grows or an undeclared root
folder fails by name, and an unknown shape is NOT CONFIRMED -- never a pass, never a
skip. No network, no scan of this tree, no sidecar, no message sent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import structure_ratchet_gate as gate
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
FLEET = 'shape = "streamlit"\n\n[structure_ratchet]\nenabled = true\nmax_file_lines = 5\n'


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PYTHONUTF8": "1"}


def _stage(root: Path) -> None:
    subprocess.run(
        [GIT or "git", "add", "-A"], cwd=root, env=_git_env(), check=True,
        creationflags=NO_WINDOW,
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A baselined throwaway repo: (the copied script, a 6-line module over the 5-line limit)."""
    assert GIT, "git is a prerequisite of the structure ratchet"
    for folder in ("app", "src", "tests", "scripts"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    script = tmp_path / "scripts" / "structure_ratchet.py"
    shutil.copy(gate.SCRIPT, script)
    for sibling in ("structure_layout.py", "structure_band.py"):
        shutil.copy(gate.SCRIPT.with_name(sibling), script.with_name(sibling))
    (tmp_path / ".fleet.toml").write_text(FLEET, encoding="utf-8")
    big = tmp_path / "src" / "big.py"
    big.write_text("A = 1\n" * 6, encoding="utf-8")
    subprocess.run(
        [GIT or "git", "init", "-q"], cwd=tmp_path, env=_git_env(), check=True,
        creationflags=NO_WINDOW,
    )
    _stage(tmp_path)
    subprocess.run(
        [sys.executable, str(script), "--init"], cwd=tmp_path, env=_git_env(), check=True,
        capture_output=True,
        creationflags=NO_WINDOW,
    )
    return script, big


def test_a_clean_tree_passes(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, _big = sandbox
    assert gate.run(script) == 0
    assert capsys.readouterr().out.rstrip().splitlines()[-1].startswith("OK: structure ratchet")


def test_a_baselined_file_that_grows_fails_by_name(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, big = sandbox
    big.write_text("A = 1\n" * 7, encoding="utf-8")
    _stage(script.parents[1])
    assert gate.run(script) == 1
    out = capsys.readouterr().out
    assert "src/big.py" in out
    assert out.rstrip().splitlines()[-1].startswith("FAIL: structure ratchet")


def test_an_undeclared_root_folder_fails_by_name(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, _big = sandbox
    stray = script.parents[1] / "vendor_pkg"
    stray.mkdir()
    (stray / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    _stage(script.parents[1])
    assert gate.run(script) == 1
    assert "vendor_pkg" in capsys.readouterr().out


def test_an_unknown_shape_is_not_confirmed(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, _big = sandbox
    (script.parents[1] / ".fleet.toml").write_text(
        FLEET.replace("streamlit", "no-such-shape"), encoding="utf-8"
    )
    assert gate.run(script) == 1
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("FAIL: structure ratchet")
    assert "NOT CONFIRMED" in last


def test_the_pre_ship_gate_runs_the_wrapper_once_after_the_comment_ratchet() -> None:
    ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [ln.strip() for ln in ps1.splitlines() if ln.lstrip().startswith("& ")]
    assert stages.count("& $python -m scripts.structure_ratchet_gate") == 1
    assert not any("structure_ratchet.py" in ln for ln in stages)
    wrapper = stages.index("& $python -m scripts.structure_ratchet_gate")
    assert stages.index("& $python -m scripts.comment_ratchet_gate") < wrapper


def test_the_ratchet_is_enabled_with_a_baseline_a_vendored_script_and_a_declared_shape() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    table = fleet["structure_ratchet"]
    assert table["enabled"] is True
    assert (REPO_ROOT / table["baseline"]).is_file()
    assert fleet["vendored"]["structure_ratchet"]["dest"] == "scripts/structure_ratchet.py"
    assert fleet["shape"] == "webapp-tray"
    assert gate.SCRIPT.is_file()
