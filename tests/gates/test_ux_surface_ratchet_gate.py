"""The gate's ux-surface-ratchet stage, driven end to end against the real script.

Each case copies the vendored `scripts/ux_surface_ratchet.py` and its two readers into a throwaway
git repo with a one-surface map and its generated page, and runs the wrapper on it, so the
exit-code mapping is checked against the script's real exits: a clean tree passes, a dialog with
no `data-surface` fails by file, and JS that never closes is NOT CONFIRMED -- never a pass, never
a skip. No network, no scan of this tree.
"""

from __future__ import annotations

import shutil
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import ux_surface_ratchet_gate as gate
from tests.gates.test_runtime_write_ratchet_gate import _run, _stage

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
TABLE = '[ux_surface_ratchet]\nenabled = true\n'
PAGE = Path("app") / "webapp" / "static" / "index.html"
MAP = (
    "app: sandbox\n"
    "walk:\n"
    "  date: 2026-01-01\n"
    "  build: abc1234\n"
    "  widths: [390]\n"
    "  themes: [light]\n"
    "areas:\n"
    "  - id: home\n"
    "    title: Home\n"
    "surfaces:\n"
    "  - id: home.sheet\n"
    "    area: home\n"
    "    title: Sheet\n"
    "    root: data-surface\n"
)
MARKED = '<dialog data-surface="home.sheet"></dialog>\n'
SIBLINGS = ("usage_track_scan.py", "ux_surface_map.py", "ratchet_baseline.py")


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A baselined throwaway repo: returns (the copied script, its page)."""
    assert GIT, "git is a prerequisite of the ux surface ratchet"
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / gate.SCRIPT.name
    for source in (gate.SCRIPT, *(gate.SCRIPT.with_name(name) for name in SIBLINGS)):
        shutil.copy(source, script.with_name(source.name))
    (tmp_path / ".fleet.toml").write_text(TABLE, encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ux-surfaces.yaml").write_text(MAP, encoding="utf-8")
    page = tmp_path / PAGE
    page.parent.mkdir(parents=True)
    page.write_text(MARKED, encoding="utf-8")
    _run(tmp_path, GIT or "git", "init", "-q")
    _stage(tmp_path)
    _run(tmp_path, sys.executable, str(script.with_name("ux_surface_map.py")))
    _stage(tmp_path)
    _run(tmp_path, sys.executable, str(script), "--init")
    return script, page


def test_a_clean_tree_passes(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, _page = sandbox
    assert gate.run(script) == 0
    assert capsys.readouterr().out.rstrip().splitlines()[-1].startswith("OK: ux surface ratchet")


def test_a_dialog_with_no_surface_id_fails_by_file(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, page = sandbox
    page.write_text(MARKED + "<dialog></dialog>\n", encoding="utf-8")
    _stage(script.parents[1])
    assert gate.run(script) == 1
    out = capsys.readouterr().out
    assert "index.html" in out
    assert out.rstrip().splitlines()[-1].startswith("FAIL: ux surface ratchet")


def test_js_that_never_closes_is_not_confirmed(
    sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    script, page = sandbox
    (page.parent / "app.js").write_text("const s = 'never closed;\n", encoding="utf-8")
    _stage(script.parents[1])
    assert gate.run(script) == 1
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("FAIL: ux surface ratchet")
    assert "NOT CONFIRMED" in last


def test_the_pre_ship_gate_runs_the_wrapper_once_after_the_usage_track_ratchet() -> None:
    ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [ln.strip() for ln in ps1.splitlines() if ln.lstrip().startswith("& ")]
    assert stages.count("& $python -m scripts.ux_surface_ratchet_gate") == 1
    wrapper = stages.index("& $python -m scripts.ux_surface_ratchet_gate")
    assert stages.index("& $python -m scripts.usage_track_ratchet_gate") < wrapper


def test_the_ratchet_is_enabled_with_a_committed_baseline_and_all_components_vendored() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    table = fleet["ux_surface_ratchet"]
    assert table["enabled"] is True
    assert (REPO_ROOT / table["baseline"]).is_file()
    assert (REPO_ROOT / table["map"]).is_file()
    vendored = fleet["vendored"]
    assert vendored["ux_surface_ratchet"]["dest"] == "scripts/ux_surface_ratchet.py"
    assert vendored["ux_surface_map"]["dest"] == "scripts/ux_surface_map.py"
    assert vendored["usage_track_scan"]["dest"] == "scripts/usage_track_scan.py"
    assert gate.SCRIPT.is_file()
