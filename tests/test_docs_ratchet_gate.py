"""The gate's docs-ratchet stage maps the vendored script's exit codes.

`scripts/docs_ratchet.py` exits 3 when it could not scan the docs: nothing was
checked. These tests pin that 3 fails as NOT CONFIRMED -- never a pass, never a
skip -- that the pre-ship gate calls the wrapper that guarantees it, and that the
real script turns red by name on a doc that gains an issue reference. They use
fake scripts and a throwaway git repo: no network, no scan of this tree, no
sidecar, no message sent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import docs_ratchet_gate as gate
from scripts import static_ratchet_gate
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SCRIPT = REPO_ROOT / "scripts" / "docs_ratchet.py"


def _exit_with(tmp_path: Path, code: int) -> Path:
    script = tmp_path / "fake_docs_ratchet.py"
    script.write_text(f"import sys\nprint('scanned')\nsys.exit({code})\n", encoding="utf-8")
    return script


def _classify(code: int) -> tuple[bool, str]:
    return static_ratchet_gate.classify_exit(code, gate._PASS_LABEL, gate._FAIL_LABELS)


def test_only_exit_zero_passes_and_three_is_not_confirmed() -> None:
    assert _classify(0)[0] is True
    assert all(not _classify(code)[0] for code in (1, 2, 3, 4, -1))
    label = _classify(3)[1]
    assert "NOT CONFIRMED" in label
    assert "skip" not in label.lower()


@pytest.mark.parametrize("code", [0, 1, 2, 3])
def test_run_fails_the_gate_for_every_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    got = gate.run(_exit_with(tmp_path, code))
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert got == (0 if code == 0 else 1)
    assert last.startswith("OK: docs ratchet" if code == 0 else "FAIL: docs ratchet")
    if code == 3:
        assert "NOT CONFIRMED" in last


def test_run_treats_a_script_that_cannot_start_as_not_confirmed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise OSError("no interpreter")

    monkeypatch.setattr(gate.run_ratchet.__globals__["subprocess"], "run", refuse)
    assert gate.run(_exit_with(tmp_path, 0)) == 1
    assert "NOT CONFIRMED" in capsys.readouterr().out


def test_the_pre_ship_gate_calls_the_wrapper_after_the_static_ratchet() -> None:
    gate_ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [line for line in gate_ps1.splitlines() if line.lstrip().startswith("& ")]
    docs = [i for i, line in enumerate(stages) if "docs_ratchet" in line]
    assert [stages[i].strip() for i in docs] == ["& $python -m scripts.docs_ratchet_gate"]
    static = next(i for i, line in enumerate(stages) if "scripts.static_ratchet_gate" in line)
    assert static < docs[0]


def test_the_ratchet_is_declared_and_its_script_is_vendored() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert fleet["docs_ratchet"]["enabled"] is True
    assert (REPO_ROOT / fleet["docs_ratchet"]["baseline"]).is_file()
    entry = fleet["vendored"]["docs_ratchet"]
    assert entry["dest"] == "scripts/docs_ratchet.py"
    assert (REPO_ROOT / entry["dest"]).is_file()


def test_a_new_issue_reference_in_a_doc_turns_the_real_script_red_by_name(
    tmp_path: Path,
) -> None:
    git = shutil.which("git")
    assert git, "git is a prerequisite of the docs ratchet"
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copy(REAL_SCRIPT, repo / "scripts" / "docs_ratchet.py")
    (repo / ".fleet.toml").write_text("[docs_ratchet]\nenabled = true\n", encoding="utf-8")
    (repo / "README.md").write_text("# Title\n\nPresent-tense text.\n", encoding="utf-8")
    doc = repo / "docs" / "topic.md"
    doc.write_text("# Topic\n\nPresent-tense text.\n", encoding="utf-8")
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PYTHONUTF8": "1"}

    def sh(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv, cwd=repo, env=env, capture_output=True, text=True, check=False,
            encoding="utf-8", stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )

    for step in (("init", "-q"), ("add", "-A")):
        assert sh(git, *step).returncode == 0
    ratchet = (sys.executable, "scripts/docs_ratchet.py")
    assert sh(*ratchet, "--init").returncode == 0
    assert sh(*ratchet).returncode == 0

    doc.write_text("# Topic\n\nAdded in #123.\n", encoding="utf-8")
    assert sh(git, "add", "-A").returncode == 0
    red = sh(*ratchet)
    assert red.returncode == 1
    assert "docs/topic.md" in red.stdout + red.stderr
    assert "#123" in red.stdout + red.stderr
