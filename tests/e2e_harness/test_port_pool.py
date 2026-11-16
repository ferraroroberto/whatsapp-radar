"""The e2e harness names a drained loopback port pool as its own failure.

A child pytest loads the real hooks from ``tests/e2e/_port_pool.py`` over three
failing tests — ``ERR_ADDRESS_IN_USE``, Playwright's ``connect ENOBUFS`` and an
ordinary one — and the output must call out only the first two.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[2]
CHILD_TIMEOUT_S = 120

_CONFTEST = """
from tests.e2e._port_pool import (  # noqa: F401
    pytest_runtest_makereport,
    pytest_terminal_summary,
)
"""
_TESTS = """
def test_drained_pool():
    raise RuntimeError("Page.goto: net::ERR_ADDRESS_IN_USE at http://127.0.0.1:1/")


def test_drained_socket_buffers():
    raise RuntimeError("APIRequestContext.get: connect ENOBUFS 127.0.0.1:1")


def test_ordinary_failure():
    raise RuntimeError("selector #record never appeared")
"""


def test_port_pool_failure_is_named_and_an_ordinary_one_is_not(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    (tmp_path / "test_child.py").write_text(_TESTS, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--rootdir", str(tmp_path), str(tmp_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CHILD_TIMEOUT_S,
        creationflags=NO_WINDOW,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"},
    )
    out = proc.stdout
    assert proc.returncode == 1, out
    # Each relabelled report and the terminal-summary section lead with the name.
    assert out.count("PORT POOL EXHAUSTED") >= 3, out
    summary = out.split("= PORT POOL EXHAUSTED =", 1)[-1].split("short test summary", 1)[0]
    assert "test_child.py::test_drained_pool" in summary, out
    assert "test_child.py::test_drained_socket_buffers" in summary, out
    assert "test_ordinary_failure" not in summary, out
