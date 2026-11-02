"""The gate's runtime-write-ratchet stage, driven end to end against the real script.

Each case copies the vendored `scripts/runtime_write_ratchet.py` and its resolver into a
throwaway git repo and runs the wrapper on it, so the exit-code mapping is checked against the
script's real exits: a clean tree passes, a new write under a code folder fails by file, and a
write target it cannot resolve is NOT CONFIRMED -- never a pass, never a skip. No network, no
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

from scripts import runtime_write_ratchet_gate as gate
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
CLEAN = "from pathlib import Path\n\nOUT = Path('data') / 'run.log'\n"


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
    assert GIT, "git is a prerequisite of the runtime write ratchet"
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src").mkdir()
    script = tmp_path / "scripts" / "runtime_write_ratchet.py"
    shutil.copy(gate.SCRIPT, script)
    shutil.copy(gate.SCRIPT.with_name("runtime_write_resolve.py"), script.with_name("runtime_write_resolve.py"))
    (tmp_path / ".fleet.toml").write_text(
        '[runtime_write_ratchet]\nenabled = true\ncode_folders = ["src"]\n', encoding="utf-8"
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
    assert capsys.readouterr().out.rstrip().splitlines()[-1].startswith("OK: runtime write ratchet")


def test_a_new_write_under_a_code_folder_fails_by_file(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, module = sandbox
    module.write_text(
        CLEAN + "\n(Path(__file__).parent / 'run.log').write_text('x')\n", encoding="utf-8"
    )
    _git(script.parents[1], "add", "-A")
    assert gate.run(script) == 1
    out = capsys.readouterr().out
    assert "mod.py" in out
    assert out.rstrip().splitlines()[-1].startswith("FAIL: runtime write ratchet")


def test_a_write_target_it_cannot_resolve_is_not_confirmed(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, module = sandbox
    module.write_text(
        CLEAN + "\n\ndef save(target: Path) -> None:\n    target.write_text('x')\n",
        encoding="utf-8",
    )
    _git(script.parents[1], "add", "-A")
    assert gate.run(script) == 1
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("FAIL: runtime write ratchet")
    assert "NOT CONFIRMED" in last


def test_the_pre_ship_gate_runs_the_wrapper_once_after_the_conventions_ratchet() -> None:
    ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [ln.strip() for ln in ps1.splitlines() if ln.lstrip().startswith("& ")]
    assert stages.count("& $python -m scripts.runtime_write_ratchet_gate") == 1
    wrapper = stages.index("& $python -m scripts.runtime_write_ratchet_gate")
    assert stages.index("& $python -m scripts.conventions_ratchet_gate") < wrapper


def test_the_pre_ship_gate_brackets_the_test_stages_with_snapshot_and_probe() -> None:
    ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    snapshot = ps1.index("runtime_write_ratchet.py --snapshot")
    probe = ps1.index("runtime_write_ratchet.py --probe")
    assert snapshot < ps1.index("& $python -m pytest")
    assert ps1.rindex("& $python -m pytest @e2eArgs") < probe


def test_the_ratchet_is_enabled_with_a_committed_baseline_and_both_components_vendored() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    table = fleet["runtime_write_ratchet"]
    assert table["enabled"] is True
    assert (REPO_ROOT / table["baseline"]).is_file()
    assert fleet["vendored"]["runtime_write_ratchet"]["dest"] == "scripts/runtime_write_ratchet.py"
    assert fleet["vendored"]["runtime_write_resolve"]["dest"] == "scripts/runtime_write_resolve.py"
    assert gate.SCRIPT.is_file()
