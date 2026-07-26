"""Follow-ups tab (#219): the /api/ack endpoints.

Offline — no connector, no LLM, no network. Mirrors test_audit_api.py's
TestClient + app.state override pattern.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from starlette.testclient import TestClient

from app.webapp.server import create_app
from src.db import store
from src.models import ChatRecord
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)
REMOTE = ("203.0.113.5", 5555)


def _client(db: Path) -> TestClient:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    return TestClient(app, client=LOOPBACK)


def _seed(conn: sqlite3.Connection) -> tuple[int, int]:
    """One pending ack item on a monitored chat. Returns (chat_id, item_id)."""
    chat_id = store.upsert_chat(
        conn,
        ChatRecord(source_chat_id="sender:school@example.test", display_name="School Updates"),
    )
    run_id = store.start_run(conn, kind="scan")
    item_id = store.insert_ack_item(
        conn, run_id, chat_id,
        child="Sam", task_category="permission_slip", summary="Bring the signed form",
    )
    return chat_id, item_id


def test_list_pending_shape(tmp_path: Path) -> None:
    db = tmp_path / "ack.sqlite3"
    conn = store.connect(db)
    _, item_id = _seed(conn)
    conn.close()

    with _client(db) as client:
        body = client.get("/api/ack/items").json()

    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == item_id
    assert item["status"] == "pending"
    assert item["display_name"] == "School Updates"
    assert item["child"] == "Sam"
    assert item["task_category"] == "permission_slip"
    assert item["summary"] == "Bring the signed form"
    assert item["calendar_event_id"] is None
    assert item["acknowledged_at"] is None


def test_acknowledge_removes_item_from_pending_list(tmp_path: Path) -> None:
    db = tmp_path / "ack.sqlite3"
    conn = store.connect(db)
    _, item_id = _seed(conn)
    conn.close()

    with _client(db) as client:
        res = client.post(f"/api/ack/{item_id}/acknowledge")
        assert res.status_code == 200
        assert res.json() == {"id": item_id, "status": "acknowledged"}

        body = client.get("/api/ack/items").json()
    assert body["items"] == []


def test_acknowledge_is_idempotent_on_repeat_tap(tmp_path: Path) -> None:
    db = tmp_path / "ack.sqlite3"
    conn = store.connect(db)
    _, item_id = _seed(conn)
    conn.close()

    with _client(db) as client:
        first = client.post(f"/api/ack/{item_id}/acknowledge")
        second = client.post(f"/api/ack/{item_id}/acknowledge")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {"id": item_id, "status": "acknowledged"}


def test_acknowledge_unknown_item_404(tmp_path: Path) -> None:
    db = tmp_path / "ack.sqlite3"
    store.connect(db).close()

    with _client(db) as client:
        res = client.post("/api/ack/9999/acknowledge")

    assert res.status_code == 404


def test_ack_requires_token_from_remote(tmp_path: Path) -> None:
    db = tmp_path / "gated.sqlite3"
    conn = store.connect(db)
    _, item_id = _seed(conn)
    conn.close()

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="secret")
    app.state.db_path = db
    with TestClient(app, client=REMOTE) as client:
        assert client.get("/api/ack/items").status_code == 401
        assert client.post(f"/api/ack/{item_id}/acknowledge").status_code == 401

        ok = client.get("/api/ack/items", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
        acked = client.post(
            f"/api/ack/{item_id}/acknowledge", headers={"Authorization": "Bearer secret"}
        )
        assert acked.status_code == 200
