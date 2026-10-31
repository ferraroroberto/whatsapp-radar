"""The CLI's stack-dump watchdog (app/cli/stack_watchdog.py).

A scheduled run that stalls is killed by the Job timeout with no traceback; the
watchdog is what makes the next one diagnosable from the Job log. The real-process
test proves the dump actually lands on stderr while the main thread is blocked —
the property that matters — rather than only that faulthandler was called.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from app.cli import stack_watchdog
from src.db.runs import FAMILY_KINDS
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_a_blocked_process_dumps_its_stack_to_stderr() -> None:
    script = textwrap.dedent(
        """
        import sys, time
        sys.path.insert(0, sys.argv[1])
        from app.cli import stack_watchdog
        stack_watchdog._VERB_BUDGET_S["probe"] = 0.5
        stack_watchdog.arm("probe")

        def stuck_in_a_blocking_wait():
            time.sleep(3)

        stuck_in_a_blocking_wait()
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=NO_WINDOW,
    )
    assert proc.returncode == 0
    assert "Timeout" in proc.stderr
    assert "stuck_in_a_blocking_wait" in proc.stderr


def test_every_family_check_gets_the_short_budget() -> None:
    """A family check finishes in seconds; a new one must not inherit the scan's
    15-minute budget and outlive its 5-minute Job timeout undumped."""
    for kind in FAMILY_KINDS:
        assert stack_watchdog.budget_for(kind) < 300
    assert stack_watchdog.budget_for("scan") > stack_watchdog.budget_for("traffic-check")


def test_the_tray_is_never_armed() -> None:
    assert stack_watchdog.arm_for_argv(["launcher.py", "tray"]) is None


def test_no_stderr_means_no_watchdog() -> None:
    """``pythonw`` has no stderr — report the watchdog off, don't pretend."""

    class _NoFd:
        def fileno(self) -> int:
            raise OSError("no fd")

    assert stack_watchdog.arm("traffic-check", stream=_NoFd()) is None  # type: ignore[arg-type]
