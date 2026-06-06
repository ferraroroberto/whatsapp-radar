"""e2e harness: self-boot a disposable webapp on a free port.

The whole suite is skipped unless ``WR_E2E_AUTOBOOT=1`` (then we boot uvicorn on
a free port and tear it down after) or a tray is already serving on :8455. This
keeps plain ``pytest`` fully offline while ``scripts/verify-before-ship.ps1``
(which sets the env var) exercises the real browser path.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TRAY_PORT = 8455


def _reachable(port: int, *, timeout: float = 0.3) -> bool:
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _autoboot() -> bool:
    return os.environ.get("WR_E2E_AUTOBOOT") == "1"


_E2E_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip the e2e suite unless we can (or already) serve the webapp.

    A sub-conftest hook still receives every collected item, so scope the skip
    to tests under this directory — never touch the offline unit suite.
    """
    if _autoboot() or _reachable(TRAY_PORT):
        return
    skip = pytest.mark.skip(
        reason="e2e disabled: set WR_E2E_AUTOBOOT=1 or run a tray on :8455"
    )
    for item in items:
        if _E2E_DIR in Path(item.fspath).parents:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def base_url() -> Iterator[str]:
    if not _autoboot():
        if _reachable(TRAY_PORT):
            yield f"http://127.0.0.1:{TRAY_PORT}"
            return
        pytest.skip("e2e disabled (no autoboot, no tray on :8455)")
        return

    port = _free_port()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Never read the developer's real WhatsApp data: point the autobooted app at
    # a throwaway empty DB (Dashboard metrics render as zeros). Honors the
    # project's hard privacy rule — e2e runs only against sanitized/empty state.
    db_dir = tempfile.mkdtemp(prefix="wr-e2e-")
    env["WR_DB_PATH"] = str(Path(db_dir) / "e2e.sqlite3")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.webapp.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("uvicorn exited before becoming ready")
            if _reachable(port):
                break
            time.sleep(0.3)
        else:
            raise RuntimeError("webapp did not become ready within 20s")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        shutil.rmtree(db_dir, ignore_errors=True)
