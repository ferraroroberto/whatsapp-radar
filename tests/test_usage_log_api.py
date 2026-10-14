"""The local-only usage log (#386): mounted behind the app's auth, ids only.

The vendored router is covered upstream; these pin what is this app's own: the
mount follows the bearer gate, the file lands where ``WR_USAGE_DB_PATH`` says,
and every ``data-track`` id in the shell is a bounded constant.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.webapp.routers._helpers import STATIC_DIR
from app.webapp.server import create_app
from src.usage_log import ID_PATTERN
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)
REMOTE = ("203.0.113.5", 5555)

BATCH = {
    "session_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
    "device_class": "phone",
    "os_class": "ios",
    "events": [
        {"kind": "session_start"},
        {"kind": "view", "id": "activity"},
        {"kind": "tap", "id": "settings-open", "view": "activity"},
    ],
}


@pytest.fixture
def usage_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "usage.sqlite3"
    monkeypatch.setenv("WR_USAGE_DB_PATH", str(target))
    return target


def _client(client: tuple[str, int], token: str = "") -> TestClient:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token=token, auth_password="")
    return TestClient(app, client=client)


def test_events_land_in_the_configured_local_file(usage_db: Path) -> None:
    res = _client(LOOPBACK).post("/api/usage/events", json=BATCH)
    assert res.status_code == 200
    assert res.json()["accepted"] == 3
    with sqlite3.connect(usage_db) as conn:
        rows = conn.execute("SELECT kind, view_id, control_id FROM events ORDER BY id").fetchall()
        columns = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
    assert rows == [("session_start", None, None), ("view", "activity", None),
                    ("tap", "activity", "settings-open")]
    assert columns == ["id", "ts", "session_id", "kind", "view_id", "control_id",
                       "device_class", "os_class"]


def test_remote_caller_needs_the_bearer_token(usage_db: Path) -> None:
    client = _client(REMOTE, token="s3cret")
    assert client.post("/api/usage/events", json=BATCH).status_code == 401
    ok = client.post(
        "/api/usage/events", json=BATCH, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


def test_a_free_text_control_id_is_dropped(usage_db: Path) -> None:
    batch = dict(BATCH, events=[{"kind": "tap", "id": "Family Group chat name", "view": "chats"}])
    res = _client(LOOPBACK).post("/api/usage/events", json=batch)
    assert res.status_code == 200
    assert res.json()["accepted"] == 0
    if usage_db.exists():  # the table is created lazily, on the first accepted event
        with sqlite3.connect(usage_db) as conn:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE name = 'events'").fetchall()
            assert not tables or conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_every_data_track_id_in_the_shell_is_a_bounded_constant() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    ids = re.findall(r'data-track="([^"]*)"', html)
    assert ids, "the shell should carry data-track ids on its main controls"
    bad = [i for i in ids if not ID_PATTERN.fullmatch(i)]
    assert not bad
