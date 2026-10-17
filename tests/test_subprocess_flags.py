"""Console-suppression flags: no ``DETACHED_PROCESS``, and a child still outlives its parent (#395).

``DETACHED_PROCESS`` and ``CREATE_NO_WINDOW`` are mutually exclusive in the Win32 docs, so the
long-lived spawns (Node sidecar, background runs) use ``NO_WINDOW_NEW_GROUP`` instead. The
survival test uses a harmless dummy child — never the real sidecar or a WhatsApp session.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from src import subprocess_flags
from src.subprocess_flags import NO_WINDOW, NO_WINDOW_NEW_GROUP

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def test_no_detached_constant_remains() -> None:
    assert not hasattr(subprocess_flags, "NO_WINDOW_DETACHED")


def test_flags_never_include_detached_process() -> None:
    assert NO_WINDOW & _DETACHED_PROCESS == 0
    assert NO_WINDOW_NEW_GROUP & _DETACHED_PROCESS == 0


@pytest.mark.skipif(sys.platform != "win32", reason="creation flags are Windows-only")
def test_new_group_flag_is_window_suppressing_group() -> None:
    assert NO_WINDOW_NEW_GROUP & subprocess.CREATE_NO_WINDOW
    assert NO_WINDOW_NEW_GROUP & _CREATE_NEW_PROCESS_GROUP


# A dummy child that touches a heartbeat file until killed; the parent snippet spawns it with the
# production flags, prints its pid, then exits at once.
_CHILD = (
    "import pathlib, sys, time\n"
    "p = pathlib.Path(sys.argv[1])\n"
    "while True:\n"
    "    p.write_text(str(time.time()))\n"
    "    time.sleep(0.1)\n"
)
_PARENT = (
    "import subprocess, sys\n"
    "from src.subprocess_flags import NO_WINDOW_NEW_GROUP\n"
    "child = subprocess.Popen(\n"
    "    [sys.executable, '-c', sys.argv[1], sys.argv[2]],\n"
    "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
    "    creationflags=NO_WINDOW_NEW_GROUP, close_fds=True,\n"
    ")\n"
    "print(child.pid)\n"
)


def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_child_outlives_parent_with_new_group_flags(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.txt"
    repo_root = Path(__file__).resolve().parents[1]
    parent = subprocess.run(
        [sys.executable, "-c", _PARENT, _CHILD, str(heartbeat)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert parent.returncode == 0, parent.stderr
    child_pid = int(parent.stdout.strip())
    try:
        # The parent has already exited; the child must still be writing its heartbeat.
        assert _wait_for(heartbeat.exists), "child never started"
        first = heartbeat.read_text(encoding="utf-8")
        assert _wait_for(lambda: heartbeat.read_text(encoding="utf-8") != first), (
            "child stopped after its parent exited"
        )
    finally:
        try:
            os.kill(child_pid, 15)  # TerminateProcess on Windows, SIGTERM elsewhere
        except OSError:
            pass
