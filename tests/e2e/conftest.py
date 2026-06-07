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


def _seed_e2e_db(db_path: Path) -> None:
    """Seed the throwaway e2e DB with SANITIZED fixtures only.

    The Chats tab needs rows to toggle / open a history overlay for. Per the
    project's hard privacy rule we use generic names ("Class 4A Group", …) and
    invented text — never real WhatsApp data. One chat starts monitored, one
    starts discovered so the toggle path has something to flip.
    """
    from src.db import store
    from src.models import ChatRecord, MessageRecord

    conn = store.connect(db_path)
    try:
        mon = store.upsert_chat(
            conn,
            ChatRecord(source_chat_id="e2e-g1", display_name="Class 4A Group", chat_type="group"),
        )
        store.upsert_chat(
            conn,
            ChatRecord(
                source_chat_id="e2e-g2", display_name="School Parents Group", chat_type="group"
            ),
        )
        store.set_chat_status(conn, mon, "monitored")
        for i in range(3):
            store.insert_message(
                conn,
                mon,
                MessageRecord(
                    source_message_id=f"e2e-a{i}",
                    message_timestamp=f"2026-06-01T10:0{i}:00+00:00",
                    text=f"sample message {i}",
                    sender_label="Parent",
                ),
            )
    finally:
        conn.close()


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
    # Execution-tab runs spawn the CLI; pin the offline stub classifier + no
    # notifier so a dry-run scan stays fast and never touches the network/hub.
    env["WR_CLASSIFIER"] = "stub"
    env["WR_CONNECTOR"] = "fixture"
    env["WR_NOTIFIER"] = "none"
    # Never read the developer's real WhatsApp data: point the autobooted app at
    # a throwaway empty DB (Dashboard metrics render as zeros). Honors the
    # project's hard privacy rule — e2e runs only against sanitized/empty state.
    db_dir = tempfile.mkdtemp(prefix="wr-e2e-")
    db_path = Path(db_dir) / "e2e.sqlite3"
    env["WR_DB_PATH"] = str(db_path)
    # Never read the developer's real WhatsApp buffer: point the sidecar status /
    # QR routes at an empty throwaway dir (renders as a 'stopped' connection).
    env["WR_LINKED_DEVICE_DIR"] = str(Path(db_dir) / "linked_device")
    _seed_e2e_db(db_path)
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
