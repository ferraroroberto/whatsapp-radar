"""The launchers live in ``scripts/``; the root keeps only ``tray.bat`` and ``setup.bat``.

The tray lifecycle helper finds a running tray by matching its command line, so the
pattern in ``tray.bat`` must keep recognising a tray started from either location —
otherwise a restart cannot replace the old-spelling process and adopts a stale build.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[2]
MOVED = ("launcher.py", "webapp.bat", "webapp_tunnel_named.bat")


def _tray_match() -> re.Pattern[str]:
    text = (REPO_ROOT / "tray.bat").read_text(encoding="utf-8")
    found = re.search(r'-TrayMatch "([^"]+)"', text)
    assert found, "tray.bat no longer passes -TrayMatch"
    return re.compile(found.group(1), re.IGNORECASE)


def test_the_moved_launchers_are_gone_from_the_root() -> None:
    for name in MOVED:
        assert not (REPO_ROOT / name).exists()
        assert (REPO_ROOT / "scripts" / name).is_file()


def test_the_tray_match_finds_both_spellings() -> None:
    pattern = _tray_match()
    assert pattern.search(r"pythonw.exe launcher.py tray")
    assert pattern.search(r"pythonw.exe scripts\launcher.py tray")
    assert pattern.search("pythonw.exe scripts/launcher.py tray")
    assert not pattern.search(r"pythonw.exe scripts\launcher.py scan")


def test_the_tray_launches_the_moved_script() -> None:
    text = (REPO_ROOT / "tray.bat").read_text(encoding="utf-8")
    assert 'set "TRAY_LAUNCH=scripts\\launcher.py tray"' in text


def test_the_moved_launcher_resolves_the_repo_root() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "launcher.py"), "no-such-command"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT / "scripts",
        creationflags=NO_WINDOW,
    )
    assert proc.returncode != 0
    assert "usage:" in proc.stderr
    assert "ModuleNotFoundError" not in proc.stderr


def test_the_moved_bats_find_the_root_again() -> None:
    for name in ("webapp.bat", "webapp_tunnel_named.bat"):
        text = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 'for %%I in ("%~dp0..") do set "SCRIPT_DIR=%%~fI\\"' in text
