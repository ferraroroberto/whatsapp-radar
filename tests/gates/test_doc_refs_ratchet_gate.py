"""The gate's doc-refs-ratchet stage, driven end to end against the real script.

Each case copies the vendored `scripts/doc_refs_ratchet.py` into a throwaway git repo
and runs the wrapper on it, so the exit-code mapping is checked against the script's
real exits: a clean tree passes, a doc naming a missing path fails by name, and a doc
the scanner cannot read is NOT CONFIRMED -- never a pass, never a skip. No network, no
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

from scripts import doc_refs_ratchet_gate as gate

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
CLEAN = "# Notes\n\nSee `scripts/doc_refs_ratchet.py` for the check.\n"


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PYTHONUTF8": "1"}


def _stage(root: Path) -> None:
    subprocess.run([GIT or "git", "add", "-A"], cwd=root, env=_git_env(), check=True)


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A baselined throwaway repo: returns (the copied script, its `README.md`)."""
    assert GIT, "git is a prerequisite of the doc refs ratchet"
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "doc_refs_ratchet.py"
    shutil.copy(gate.SCRIPT, script)
    fleet = "[doc_refs_ratchet]\nenabled = true\n"
    (tmp_path / ".fleet.toml").write_text(fleet, encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(CLEAN, encoding="utf-8")
    subprocess.run([GIT or "git", "init", "-q"], cwd=tmp_path, env=_git_env(), check=True)
    _stage(tmp_path)
    subprocess.run(
        [sys.executable, str(script), "--init"], cwd=tmp_path, env=_git_env(), check=True,
        capture_output=True,
    )
    return script, readme


def test_a_clean_tree_passes(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, _readme = sandbox
    assert gate.run(script) == 0
    assert capsys.readouterr().out.rstrip().splitlines()[-1].startswith("OK: doc refs ratchet")


def test_a_reference_to_a_missing_path_fails_by_doc_and_path(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, readme = sandbox
    readme.write_text(CLEAN + "\nThe helper is `scripts/no_such_helper.py`.\n", encoding="utf-8")
    _stage(script.parents[1])
    assert gate.run(script) == 1
    out = capsys.readouterr().out
    assert "README.md" in out
    assert "scripts/no_such_helper.py" in out
    assert out.rstrip().splitlines()[-1].startswith("FAIL: doc refs ratchet")


def test_a_doc_the_scanner_cannot_read_is_not_confirmed(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, readme = sandbox
    readme.write_bytes(b"# Notes\n\n\xff\xfe not utf-8\n")
    _stage(script.parents[1])
    assert gate.run(script) == 1
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("FAIL: doc refs ratchet")
    assert "NOT CONFIRMED" in last


def test_the_pre_ship_gate_runs_the_wrapper_once_after_the_instruction_ratchet() -> None:
    ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [ln.strip() for ln in ps1.splitlines() if ln.lstrip().startswith("& ")]
    assert stages.count("& $python -m scripts.doc_refs_ratchet_gate") == 1
    assert not any("doc_refs_ratchet.py" in ln for ln in stages)
    wrapper = stages.index("& $python -m scripts.doc_refs_ratchet_gate")
    assert stages.index("& $python -m scripts.instruction_ratchet_gate") < wrapper


def test_the_ratchet_is_enabled_with_a_committed_baseline_and_a_vendored_script() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    table = fleet["doc_refs_ratchet"]
    assert table["enabled"] is True
    assert (REPO_ROOT / table["baseline"]).is_file()
    assert fleet["vendored"]["doc_refs_ratchet"]["dest"] == "scripts/doc_refs_ratchet.py"
    assert gate.SCRIPT.is_file()
