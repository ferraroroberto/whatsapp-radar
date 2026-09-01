"""task-os Inbox export (#307): client logic + the webapp endpoint.

Fully offline. The client tests stub :func:`src._loopback_http.request`; the
endpoint tests inject a fake exporter via ``app.state.task_os_exporter`` so
task-os is never dialled. Mirrors ``tests/test_summarize.py``'s pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.webapp.server import create_app
from src import _loopback_http
from src.config import TaskOsConfig
from src.db import store
from src.models import ChatRecord, MessageRecord, TaskExportContext
from src.task_os import client as task_os_client
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)

CONFIGURED = TaskOsConfig(enabled=True, base_url="http://127.0.0.1:8448", token="secret")

CONTEXT = TaskExportContext(
    source_message_id="wa-1",
    text="Please bring the signed permission form tomorrow.",
    sender_label="Teacher",
    chat_name="Class 4A Group",
    message_timestamp="2026-06-10T10:00:00+00:00",
    task_exported_at=None,
)


# --- client: payload / config gate / errors ---------------------------------

def test_build_title_collapses_whitespace_and_caps_length() -> None:
    long_text = "word " * 40
    ctx = TaskExportContext(
        source_message_id="m", text=long_text, sender_label=None,
        chat_name="c", message_timestamp="t", task_exported_at=None,
    )
    title = task_os_client.build_title(ctx)
    assert len(title) <= 120
    assert title.endswith("…")
    assert "  " not in title


def test_build_title_short_text_unchanged() -> None:
    assert task_os_client.build_title(CONTEXT) == CONTEXT.text


def test_build_description_includes_sender_chat_and_full_text() -> None:
    desc = task_os_client.build_description(CONTEXT)
    assert "Teacher" in desc
    assert "Class 4A Group" in desc
    assert CONTEXT.text in desc


def test_build_description_falls_back_for_missing_sender() -> None:
    ctx = TaskExportContext(
        source_message_id="m", text="x", sender_label=None,
        chat_name="c", message_timestamp="t", task_exported_at=None,
    )
    assert "Unknown sender" in task_os_client.build_description(ctx)


def test_build_task_payload_shape() -> None:
    payload = task_os_client.build_task_payload(CONTEXT)
    assert payload["title"] == CONTEXT.text
    assert payload["external_id"] == "wa-1"
    assert payload["actor"] == "whatsapp-radar"
    assert "description" in payload


def test_export_message_raises_when_disabled() -> None:
    with pytest.raises(task_os_client.TaskOsNotConfigured):
        task_os_client.export_message(TaskOsConfig(enabled=False), CONTEXT)


def test_export_message_raises_when_token_missing() -> None:
    with pytest.raises(task_os_client.TaskOsNotConfigured):
        task_os_client.export_message(TaskOsConfig(enabled=True, token=""), CONTEXT)


def test_export_message_posts_and_returns_task(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        seen["method"], seen["url"], seen["kwargs"] = method, url, kwargs
        return {"id": 42, "title": kwargs["json"]["title"]}

    monkeypatch.setattr(_loopback_http, "request", fake_request)
    result = task_os_client.export_message(CONFIGURED, CONTEXT)
    assert result == {"id": 42, "title": CONTEXT.text}
    assert seen["method"] == "POST"
    assert seen["url"] == "http://127.0.0.1:8448/api/tasks"
    assert seen["kwargs"]["headers"] == {"Authorization": "Bearer secret"}


def test_export_message_surfaces_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(*_a: Any, **_k: Any) -> Any:
        raise task_os_client.TaskOsError("task-os unreachable", status=503)

    monkeypatch.setattr(_loopback_http, "request", fake_request)
    with pytest.raises(task_os_client.TaskOsError):
        task_os_client.export_message(CONFIGURED, CONTEXT)


# --- endpoint: 200 / 400 / 404 / upstream error / persistence --------------

def _seed_db(db: Path) -> dict[str, int]:
    conn = store.connect(db)
    chat = store.upsert_chat(
        conn, ChatRecord(source_chat_id="c", display_name="Class 4A Group", chat_type="group")
    )
    store.insert_message(
        conn, chat,
        MessageRecord(source_message_id="m1", message_timestamp="2026-06-10T10:00:00+00:00",
                      text="Please bring the signed form tomorrow.", sender_label="Teacher"),
    )
    store.insert_message(
        conn, chat,
        MessageRecord(source_message_id="m2", message_timestamp="2026-06-10T10:01:00+00:00",
                      text=None, sender_label="Parent", message_type="voice"),
    )

    def _id(src: str) -> int:
        return int(
            conn.execute(
                "SELECT id FROM messages WHERE source_message_id = ?", (src,)
            ).fetchone()["id"]
        )

    ids = {"textual": _id("m1"), "textless": _id("m2"), "chat": chat}
    conn.close()
    return ids


def _app_with_db(db: Path, exporter: Any) -> Any:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    app.state.task_os_exporter = exporter
    app.state.task_os_config = CONFIGURED
    return app


def test_export_endpoint_returns_and_persists(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    ids = _seed_db(db)
    seen: dict[str, Any] = {}

    def fake_exporter(config: TaskOsConfig, ctx: TaskExportContext) -> dict[str, Any]:
        seen["config"], seen["ctx"] = config, ctx
        return {"id": 1}

    with TestClient(_app_with_db(db, fake_exporter), client=LOOPBACK) as client:
        r = client.post(f"/api/messages/{ids['textual']}/task-export")
        assert r.status_code == 200
        body = r.json()
        assert body["message_id"] == ids["textual"]
        assert body["exported_at"]

    assert seen["config"] == CONFIGURED
    assert seen["ctx"].source_message_id == "m1"
    assert seen["ctx"].sender_label == "Teacher"
    assert seen["ctx"].chat_name == "Class 4A Group"

    conn = store.connect(db)
    try:
        ctx = store.message_task_export_context(conn, ids["textual"])
        assert ctx is not None
        assert ctx.task_exported_at is not None
    finally:
        conn.close()


def test_export_endpoint_idempotent_second_tap_does_not_repost(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    ids = _seed_db(db)
    calls = 0

    def counting_exporter(_c: TaskOsConfig, _ctx: TaskExportContext) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"id": 1}

    with TestClient(_app_with_db(db, counting_exporter), client=LOOPBACK) as client:
        first = client.post(f"/api/messages/{ids['textual']}/task-export")
        assert first.status_code == 200
        assert calls == 1

        second = client.post(f"/api/messages/{ids['textual']}/task-export")
        assert second.status_code == 200
        assert second.json()["exported_at"] == first.json()["exported_at"]
        assert calls == 1  # not re-posted — read-through on the persisted timestamp


def test_export_endpoint_404_for_missing(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    _seed_db(db)

    def boom(_c: Any, _ctx: Any) -> Any:
        raise AssertionError("exporter dialled for a missing message")

    with TestClient(_app_with_db(db, boom), client=LOOPBACK) as client:
        assert client.post("/api/messages/99999/task-export").status_code == 404


def test_export_endpoint_404_for_textless_message(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    ids = _seed_db(db)

    def boom(_c: Any, _ctx: Any) -> Any:
        raise AssertionError("exporter dialled for a message with no text")

    with TestClient(_app_with_db(db, boom), client=LOOPBACK) as client:
        assert client.post(f"/api/messages/{ids['textless']}/task-export").status_code == 404


def test_export_endpoint_surfaces_not_configured(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    ids = _seed_db(db)
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    app.state.task_os_config = TaskOsConfig(enabled=False)
    # No app.state.task_os_exporter override — the real client raises
    # TaskOsNotConfigured before ever attempting a network call.

    with TestClient(app, client=LOOPBACK) as client:
        r = client.post(f"/api/messages/{ids['textual']}/task-export")
        assert r.status_code == 400
        assert "not configured" in r.json()["detail"]


def test_export_endpoint_surfaces_upstream_error(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    ids = _seed_db(db)

    def down(_c: Any, _ctx: Any) -> Any:
        raise task_os_client.TaskOsError("task-os unreachable", status=503)

    with TestClient(_app_with_db(db, down), client=LOOPBACK) as client:
        r = client.post(f"/api/messages/{ids['textual']}/task-export")
        assert r.status_code == 503
        assert "unreachable" in r.json()["detail"]


def test_task_exported_at_exposed_in_chat_history(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    ids = _seed_db(db)

    with TestClient(
        _app_with_db(db, lambda _c, _ctx: {"id": 1}), client=LOOPBACK
    ) as client:
        client.post(f"/api/messages/{ids['textual']}/task-export")
        history = client.get(f"/api/chats/{ids['chat']}/history").json()

    by_id = {m["id"]: m for m in history["messages"]}
    assert by_id[ids["textual"]]["task_exported_at"] is not None


def test_message_task_export_context_none_for_missing_and_textless(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    ids = _seed_db(db)
    conn = store.connect(db)
    try:
        assert store.message_task_export_context(conn, 99999) is None
        assert store.message_task_export_context(conn, ids["textless"]) is None
    finally:
        conn.close()
