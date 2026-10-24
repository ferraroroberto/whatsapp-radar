"""The gate's comment-ratchet stage, driven end to end against the real script.

Each case copies the vendored `scripts/comment_ratchet.py` into a throwaway git repo
and runs the wrapper on it, so the exit-code mapping is checked against the script's
real exits: a clean tree passes, a new issue reference in a comment fails by name,
and a file the scanner cannot read is NOT CONFIRMED -- never a pass, never a skip.
No network, no scan of this tree, no sidecar, no message sent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import comment_ratchet_gate as gate
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PYTHONUTF8": "1"}


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A baselined throwaway repo: returns (the copied script, the module under test)."""
    assert GIT, "git is a prerequisite of the comment ratchet"
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "comment_ratchet.py"
    shutil.copy(gate.SCRIPT, script)
    (tmp_path / ".fleet.toml").write_text("[comment_ratchet]\nenabled = true\n", encoding="utf-8")
    module = tmp_path / "src" / "topic.py"
    module.write_text("# Present-tense reason.\nVALUE = 1\n", encoding="utf-8")
    for argv in (["init", "-q"], ["add", "-A"]):
        subprocess.run(
            [GIT or "git", *argv], cwd=tmp_path, env=_git_env(), check=True,
            creationflags=NO_WINDOW,
        )
    subprocess.run(
        [sys.executable, str(script), "--init"], cwd=tmp_path, env=_git_env(), check=True,
        capture_output=True,
        creationflags=NO_WINDOW,
    )
    return script, module


def _stage_and_run(script: Path, module: Path, body: str) -> int:
    module.write_text(body, encoding="utf-8")
    subprocess.run(
        [GIT or "git", "add", "-A"], cwd=script.parents[1], env=_git_env(), check=True,
        creationflags=NO_WINDOW,
    )
    return gate.run(script)


def test_a_clean_tree_passes(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, _module = sandbox
    assert gate.run(script) == 0
    assert capsys.readouterr().out.rstrip().splitlines()[-1].startswith("OK: comment ratchet")


def test_a_new_issue_reference_fails_by_file_and_reference(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, module = sandbox
    assert _stage_and_run(script, module, "# Added in #123.\nVALUE = 1\n") == 1
    out = capsys.readouterr().out
    assert "src/topic.py" in out
    assert "#123" in out
    assert out.rstrip().splitlines()[-1].startswith("FAIL: comment ratchet")


def test_a_file_the_scanner_cannot_read_is_not_confirmed(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, module = sandbox
    assert _stage_and_run(script, module, 'VALUE = """never closed\n') == 1
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("FAIL: comment ratchet")
    assert "NOT CONFIRMED" in last


def test_a_script_that_cannot_start_is_not_confirmed(
    sandbox: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise OSError("no interpreter")

    monkeypatch.setattr(gate.run_ratchet.__globals__["subprocess"], "run", refuse)
    assert gate.run(sandbox[0]) == 1
    assert "NOT CONFIRMED" in capsys.readouterr().out


def test_the_pre_ship_gate_runs_the_wrapper_once_after_the_docs_ratchet() -> None:
    ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [ln.strip() for ln in ps1.splitlines() if ln.lstrip().startswith("& ")]
    assert stages.count("& $python -m scripts.comment_ratchet_gate") == 1
    assert not any("comment_ratchet.py" in ln for ln in stages)
    wrapper = stages.index("& $python -m scripts.comment_ratchet_gate")
    assert stages.index("& $python -m scripts.docs_ratchet_gate") < wrapper


def test_the_ratchet_is_enabled_with_a_committed_baseline_and_a_vendored_script() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    table = fleet["comment_ratchet"]
    assert table["enabled"] is True
    assert (REPO_ROOT / table["baseline"]).is_file()
    assert fleet["vendored"]["comment_ratchet"]["dest"] == "scripts/comment_ratchet.py"
    assert gate.SCRIPT.is_file()
