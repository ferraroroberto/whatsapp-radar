"""Chats & Config tab (#10): store listing/history + the /api/chats and
/api/config endpoints.

Store helpers are asserted against a hand-seeded fixture DB so the numbers are
known; the endpoints check JSON shape, the monitor→baseline guarantee, the
bearer gate, and that the Telegram token is never returned (or overwritten with)
a blank.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.webapp.routers import chats
from app.webapp.server import create_app
from src.db import store
from src.models import ChatRecord, MessageRecord
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)
REMOTE = ("203.0.113.5", 5555)


def _seed(conn: sqlite3.Connection) -> tuple[int, int]:
    """One monitored chat (3 msgs, older) + one discovered chat (2 msgs, newer).

    Returns (monitored_id, discovered_id).
    """
    mon = store.upsert_chat(
        conn, ChatRecord(source_chat_id="g1", display_name="Class 4A Group", chat_type="group")
    )
    disc = store.upsert_chat(
        conn,
        ChatRecord(source_chat_id="g2", display_name="School Parents Group", chat_type="group"),
    )
    store.set_chat_status(conn, mon, "monitored")

    for i in range(3):
        store.insert_message(
            conn,
            mon,
            MessageRecord(
                source_message_id=f"a{i}",
                message_timestamp=f"2026-06-01T10:0{i}:00+00:00",
                text=f"older {i}",
                sender_label="X",
            ),
        )
    for i in range(2):
        store.insert_message(
            conn,
            disc,
            MessageRecord(
                source_message_id=f"b{i}",
                message_timestamp=f"2026-06-01T11:0{i}:00+00:00",
                text=f"newer {i}",
                sender_label="Y",
            ),
        )
    return mon, disc


# --- store helpers ----------------------------------------------------------

def test_chats_overview_fields_and_order(conn: sqlite3.Connection) -> None:
    _seed(conn)
    rows = store.chats_overview(conn)
    assert len(rows) == 2
    # Most recently active first: School Parents (11:xx) before Class 4A (10:xx).
    assert rows[0]["display_name"] == "School Parents Group"
    assert rows[0]["message_count"] == 2
    assert rows[0]["last_message_text"] == "newer 1"
    assert rows[1]["display_name"] == "Class 4A Group"
    assert rows[1]["message_count"] == 3
    assert rows[1]["last_message_text"] == "older 2"
    assert rows[1]["status"] == "monitored"


def test_chats_overview_empty(conn: sqlite3.Connection) -> None:
    assert store.chats_overview(conn) == []


def test_recent_messages_limit_and_order(conn: sqlite3.Connection) -> None:
    chat = store.upsert_chat(
        conn, ChatRecord(source_chat_id="c", display_name="C", chat_type="group")
    )
    for i in range(5):
        store.insert_message(
            conn,
            chat,
            MessageRecord(
                source_message_id=f"m{i}",
                message_timestamp=f"2026-06-01T10:0{i}:00+00:00",
                text=f"msg {i}",
                sender_label="X",
            ),
        )
    recent, has_more = store.recent_messages(conn, chat, limit=3)
    # Newest 3, returned oldest→newest; two older remain.
    assert [m.text for m in recent] == ["msg 2", "msg 3", "msg 4"]
    assert has_more is True


def test_recent_messages_keyset_pagination(conn: sqlite3.Connection) -> None:
    chat = store.upsert_chat(
        conn, ChatRecord(source_chat_id="c", display_name="C", chat_type="group")
    )
    for i in range(5):
        store.insert_message(
            conn,
            chat,
            MessageRecord(
                source_message_id=f"m{i}",
                message_timestamp=f"2026-06-01T10:0{i}:00+00:00",
                text=f"msg {i}",
                sender_label="X",
            ),
        )
    page1, more1 = store.recent_messages(conn, chat, limit=2)
    assert [m.text for m in page1] == ["msg 3", "msg 4"] and more1 is True

    oldest = page1[0]
    page2, more2 = store.recent_messages(
        conn, chat, limit=2, before_ts=oldest.message_timestamp, before_id=oldest.id
    )
    assert [m.text for m in page2] == ["msg 1", "msg 2"] and more2 is True

    oldest = page2[0]
    page3, more3 = store.recent_messages(
        conn, chat, limit=2, before_ts=oldest.message_timestamp, before_id=oldest.id
    )
    assert [m.text for m in page3] == ["msg 0"] and more3 is False


def test_get_chat(conn: sqlite3.Connection) -> None:
    mon, _ = _seed(conn)
    row = store.get_chat(conn, mon)
    assert row is not None and row["display_name"] == "Class 4A Group"
    assert store.get_chat(conn, 99999) is None


def test_set_chat_alias_set_and_clear(conn: sqlite3.Connection) -> None:
    mon, _ = _seed(conn)
    assert store.get_chat(conn, mon)["alias"] is None

    assert store.set_chat_alias(conn, mon, "  Tom  ") is True  # trimmed
    assert store.get_chat(conn, mon)["alias"] == "Tom"
    # The alias also surfaces in the overview listing the webapp renders.
    overview = {r["id"]: r for r in store.chats_overview(conn)}
    assert overview[mon]["alias"] == "Tom"

    # Whitespace-only and None both clear back to NULL.
    assert store.set_chat_alias(conn, mon, "   ") is True
    assert store.get_chat(conn, mon)["alias"] is None
    store.set_chat_alias(conn, mon, "Tom")
    assert store.set_chat_alias(conn, mon, None) is True
    assert store.get_chat(conn, mon)["alias"] is None


# --- /api/chats endpoints ---------------------------------------------------

def _app_with_db(db: Path, *, token: str = "", buffer: Path | None = None) -> Any:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token=token)
    app.state.db_path = db
    if buffer is not None:
        app.state.linked_device_dir = buffer
    return app


def test_list_chats_endpoint(tmp_path: Path) -> None:
    db = tmp_path / "chats.sqlite3"
    conn = store.connect(db)
    _seed(conn)
    conn.close()

    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        body = client.get("/api/chats").json()

    names = [c["name"] for c in body["chats"]]
    assert names == ["School Parents Group", "Class 4A Group"]
    first = body["chats"][0]
    assert {"id", "name", "type", "status", "count", "last_message_at", "last_message_text"} <= set(
        first
    )


def test_history_endpoint(tmp_path: Path) -> None:
    db = tmp_path / "hist.sqlite3"
    conn = store.connect(db)
    mon, _ = _seed(conn)
    conn.close()

    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        body = client.get(f"/api/chats/{mon}/history?limit=100").json()

    assert body["name"] == "Class 4A Group"
    assert [m["text"] for m in body["messages"]] == ["older 0", "older 1", "older 2"]
    assert body["has_more"] is False
    # 404 for an unknown chat.
    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        assert client.get("/api/chats/99999/history").status_code == 404


# --- voice-note audio playback endpoint (#86) -------------------------------

def _seed_voice(conn: sqlite3.Connection, buffer: Path, *, media_path: str | None) -> int:
    """Insert a transcribed voice note and return its message id.

    With ``media_path`` set, a stub audio file is dropped under ``buffer`` and the
    row retains its path (playable). ``None`` models a swept/never-downloaded note.
    """
    chat = store.upsert_chat(conn, ChatRecord(source_chat_id="v", display_name="Class 4A Group"))
    # insert_messages (plural) persists transcription_status + media_path; the
    # singular insert_message omits them.
    store.insert_messages(
        conn,
        chat,
        [
            MessageRecord(
                source_message_id="vn1",
                message_timestamp="2026-06-01T10:00:00+00:00",
                text="[voice note]",
                sender_label="X",
                message_type="voice",
                transcription_status="done",
                media_path=media_path,
            )
        ],
    )
    if media_path is not None:
        f = buffer / media_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"OggS-fake-audio")
    return conn.execute(
        "SELECT id FROM messages WHERE source_message_id = 'vn1'"
    ).fetchone()["id"]


def conn_chat_id(db: Path) -> int:
    conn = store.connect(db)
    try:
        return conn.execute("SELECT id FROM chats LIMIT 1").fetchone()["id"]
    finally:
        conn.close()


def test_audio_endpoint_streams_wav_passthrough(tmp_path: Path) -> None:
    # A WAV note is served as-is (no transcode) with Range support.
    db, buffer = tmp_path / "a.sqlite3", tmp_path / "buf"
    conn = store.connect(db)
    mid = _seed_voice(conn, buffer, media_path="media/vn1.wav")
    conn.close()

    with TestClient(_app_with_db(db, buffer=buffer), client=LOOPBACK) as client:
        r = client.get(f"/api/messages/{mid}/audio")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
        assert r.content == b"OggS-fake-audio"
        # The history response flags the note as playable.
        chat = conn_chat_id(db)
        body = client.get(f"/api/chats/{chat}/history").json()
        assert body["messages"][0]["has_audio"] is True


def test_audio_endpoint_transcodes_ogg_to_mp3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WhatsApp voice notes are OGG/Opus (unplayable in iOS Safari), so the endpoint
    # transcodes to MP3 via ffmpeg. ffmpeg is faked so the test stays offline.
    db, buffer = tmp_path / "a.sqlite3", tmp_path / "buf"
    conn = store.connect(db)
    mid = _seed_voice(conn, buffer, media_path="media/vn1.ogg")
    conn.close()

    monkeypatch.setattr(chats.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    ran: dict[str, Any] = {}

    class _Proc:
        stdout = b"ID3-fake-mp3"

    def _fake_run(cmd: list[str], **kw: Any) -> _Proc:
        ran["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(chats.subprocess, "run", _fake_run)

    with TestClient(_app_with_db(db, buffer=buffer), client=LOOPBACK) as client:
        r = client.get(f"/api/messages/{mid}/audio")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/mpeg"
        assert r.content == b"ID3-fake-mp3"
    assert "ffmpeg" in str(ran["cmd"][0]) and "mp3" in ran["cmd"]


def test_audio_endpoint_falls_back_to_original_when_ffmpeg_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No ffmpeg → serve the original OGG so non-Safari clients still play it.
    db, buffer = tmp_path / "a.sqlite3", tmp_path / "buf"
    conn = store.connect(db)
    mid = _seed_voice(conn, buffer, media_path="media/vn1.ogg")
    conn.close()

    monkeypatch.setattr(chats.shutil, "which", lambda _name: None)

    with TestClient(_app_with_db(db, buffer=buffer), client=LOOPBACK) as client:
        r = client.get(f"/api/messages/{mid}/audio")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/ogg"
        assert r.content == b"OggS-fake-audio"


def test_audio_endpoint_404_when_swept(tmp_path: Path) -> None:
    db, buffer = tmp_path / "a.sqlite3", tmp_path / "buf"
    conn = store.connect(db)
    mid = _seed_voice(conn, buffer, media_path=None)  # audio gone / never retained
    conn.close()

    with TestClient(_app_with_db(db, buffer=buffer), client=LOOPBACK) as client:
        assert client.get(f"/api/messages/{mid}/audio").status_code == 404
        assert client.get("/api/messages/99999/audio").status_code == 404


def test_audio_endpoint_404_when_file_missing(tmp_path: Path) -> None:
    # media_path is set but the file isn't on disk (cleared out of band).
    db, buffer = tmp_path / "a.sqlite3", tmp_path / "buf"
    conn = store.connect(db)
    mid = _seed_voice(conn, buffer, media_path="media/vn1.ogg")
    (buffer / "media" / "vn1.ogg").unlink()
    conn.close()

    with TestClient(_app_with_db(db, buffer=buffer), client=LOOPBACK) as client:
        assert client.get(f"/api/messages/{mid}/audio").status_code == 404


def test_audio_endpoint_rejects_path_traversal(tmp_path: Path) -> None:
    # A media_path escaping the buffer dir must never be served, even if the file
    # exists outside it.
    db, buffer = tmp_path / "a.sqlite3", tmp_path / "buf"
    buffer.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top secret")
    conn = store.connect(db)
    mid = _seed_voice(conn, buffer, media_path="../secret.txt")
    conn.close()

    with TestClient(_app_with_db(db, buffer=buffer), client=LOOPBACK) as client:
        assert client.get(f"/api/messages/{mid}/audio").status_code == 404


def test_history_endpoint_paginates(tmp_path: Path) -> None:
    db = tmp_path / "histpage.sqlite3"
    conn = store.connect(db)
    mon, _ = _seed(conn)  # the monitored chat has 3 messages
    conn.close()

    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        first = client.get(f"/api/chats/{mon}/history?limit=2").json()
        assert [m["text"] for m in first["messages"]] == ["older 1", "older 2"]
        assert first["has_more"] is True

        oldest = first["messages"][0]
        older = client.get(
            f"/api/chats/{mon}/history?limit=2"
            f"&before_ts={oldest['ts']}&before_id={oldest['id']}"
        ).json()
        assert [m["text"] for m in older["messages"]] == ["older 0"]
        assert older["has_more"] is False


def test_status_monitor_baselines_cursor(tmp_path: Path) -> None:
    db = tmp_path / "toggle.sqlite3"
    conn = store.connect(db)
    _, disc = _seed(conn)
    conn.close()

    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        res = client.post(f"/api/chats/{disc}/status", json={"status": "monitored"})
        assert res.status_code == 200
        assert res.json() == {"id": disc, "status": "monitored", "baselined": True}

    conn = store.connect(db)
    try:
        assert store.get_chat(conn, disc)["status"] == "monitored"
        # The cursor was baselined so the first review skips the existing backlog.
        assert store.messages_since_cursor(conn, disc) == []
        assert (
            conn.execute(
                "SELECT 1 FROM chat_review_state WHERE chat_id = ?", (disc,)
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_status_ignore_and_validation(tmp_path: Path) -> None:
    db = tmp_path / "ignore.sqlite3"
    conn = store.connect(db)
    mon, _ = _seed(conn)
    conn.close()

    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        ok = client.post(f"/api/chats/{mon}/status", json={"status": "ignored"})
        assert ok.status_code == 200 and ok.json()["baselined"] is False
        # Bad status → 400; unknown chat → 404.
        assert client.post(f"/api/chats/{mon}/status", json={"status": "bogus"}).status_code == 400
        assert client.post("/api/chats/99999/status", json={"status": "ignored"}).status_code == 404

    conn = store.connect(db)
    try:
        assert store.get_chat(conn, mon)["status"] == "ignored"
    finally:
        conn.close()


def test_alias_endpoint_sets_clears_and_validates(tmp_path: Path) -> None:
    db = tmp_path / "alias.sqlite3"
    conn = store.connect(db)
    mon, _ = _seed(conn)
    conn.close()

    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        # Set an alias; it comes back trimmed and shows up in the listing.
        res = client.post(f"/api/chats/{mon}/alias", json={"alias": "  Tom  "})
        assert res.status_code == 200 and res.json() == {"id": mon, "alias": "Tom"}
        listed = {c["id"]: c for c in client.get("/api/chats").json()["chats"]}
        assert listed[mon]["alias"] == "Tom"
        assert client.get(f"/api/chats/{mon}/history").json()["alias"] == "Tom"

        # Blank clears it; an over-long value is capped at 100 chars.
        assert client.post(f"/api/chats/{mon}/alias", json={"alias": ""}).json()["alias"] is None
        capped = client.post(f"/api/chats/{mon}/alias", json={"alias": "x" * 250}).json()["alias"]
        assert capped is not None and len(capped) == 100

        # Unknown chat → 404.
        assert client.post("/api/chats/99999/alias", json={"alias": "x"}).status_code == 404


def test_chats_requires_token_from_remote(tmp_path: Path) -> None:
    db = tmp_path / "gated.sqlite3"
    store.connect(db).close()

    with TestClient(_app_with_db(db, token="secret"), client=REMOTE) as client:
        assert client.get("/api/chats").status_code == 401
        ok = client.get("/api/chats", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200


# --- /api/config endpoints --------------------------------------------------

def test_get_config_masks_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.webapp.routers.config as config_router

    # This assertion covers committed defaults, independent of ignored local sources.
    monkeypatch.setenv("WR_SOURCES", "whatsapp")
    monkeypatch.setattr(
        config_router,
        "load_webapp_config",
        lambda: WebappConfig(telegram_bot_token="123456789secret", telegram_chat_id="42"),
    )
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    with TestClient(app, client=LOOPBACK) as client:
        body = client.get("/api/config").json()

    assert body["prompt"].strip()  # the system prompt renders
    assert body["keyword_roots"].strip()  # the roots file renders
    assert body["settings"]["connector"]
    assert body["settings"]["sources"] == ["whatsapp"]
    assert set(body["options"]["sources"]) == {"gmail", "whatsapp"}
    assert "telegram" in body["options"]["notifier"]
    # Token is never returned in clear — only configured + a last-4 hint.
    tok = body["telegram"]["token"]
    assert tok == {"configured": True, "hint": "…cret"}
    assert "123456789secret" not in str(body)
    assert body["telegram"]["chat_id"] == "42"


def test_post_config_routes_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.webapp.routers.config as config_router

    saved_local: dict[str, Any] = {}
    saved_tg: dict[str, Any] = {}

    def fake_local(partial: dict[str, Any]) -> Path:
        saved_local.update(partial)
        return Path("local.json")

    def fake_tg(**fields: Any) -> WebappConfig:
        saved_tg.update(fields)
        return WebappConfig()

    monkeypatch.setattr(config_router, "save_local_overrides", fake_local)
    monkeypatch.setattr(config_router, "update_webapp_config", fake_tg)

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    with TestClient(app, client=LOOPBACK) as client:
        res = client.post(
            "/api/config",
            json={
                "connector": "linked_device",
                "sources": ["whatsapp", "gmail"],
                "classifier": "cascade",
                "notifier": "telegram",
                "hub_base_url": "http://127.0.0.1:8000",
                "hub_model": "claude_sonnet",
                "telegram_chat_id": "99",
                # blank token must NOT be forwarded to update_webapp_config
                "telegram_bot_token": "",
            },
        )
        assert res.status_code == 200

    assert saved_local["connector"] == "linked_device"
    assert saved_local["sources"] == ["whatsapp", "gmail"]
    assert saved_local["classifier"] == "cascade"
    assert saved_local["notifier"] == "telegram"
    assert saved_local["hub"] == {"base_url": "http://127.0.0.1:8000", "model": "claude_sonnet"}
    # chat_id forwarded; blank token suppressed.
    assert saved_tg == {"telegram_chat_id": "99"}


def test_post_config_rejects_bad_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.webapp.routers.config as config_router

    monkeypatch.setattr(config_router, "save_local_overrides", lambda partial: Path("x"))
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    with TestClient(app, client=LOOPBACK) as client:
        assert client.post("/api/config", json={"connector": "bogus"}).status_code == 400
        assert client.post("/api/config", json={"sources": []}).status_code == 400
        assert client.post("/api/config", json={"sources": ["sms"]}).status_code == 400


def test_post_config_persists_token_when_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.webapp.routers.config as config_router

    saved_tg: dict[str, Any] = {}
    monkeypatch.setattr(
        config_router, "update_webapp_config", lambda **f: saved_tg.update(f) or WebappConfig()
    )
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    with TestClient(app, client=LOOPBACK) as client:
        res = client.post("/api/config", json={"telegram_bot_token": "newtoken123"})
        assert res.status_code == 200
    assert saved_tg == {"telegram_bot_token": "newtoken123"}
