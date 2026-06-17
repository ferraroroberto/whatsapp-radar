"""On-demand message summarize (#86 Part B): client logic + the webapp endpoint.

Fully offline. The client tests stub :func:`src._loopback_http.request`; the
endpoint tests inject a fake summarizer via ``app.state.summarizer`` so no hub is
ever dialled. Mirrors App Launcher's hub-client pattern (reused, not rebuilt).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.webapp.server import create_app
from src import _loopback_http
from src.analysis import summarize as summarize_client
from src.db import store
from src.models import ChatRecord, MessageRecord
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)

_LONG = "Reminder for tomorrow: " + "please bring the signed form and 12 euros. " * 8
_SHORT = "ok thanks"


# --- client: payload / extraction / errors ---------------------------------

def test_build_summary_payload_shape() -> None:
    payload = summarize_client.build_summary_payload("hello", model="claude_haiku")
    assert payload["model"] == "claude_haiku"
    assert payload["stream"] is False
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["system", "user"]
    assert payload["messages"][1]["content"] == "hello"


def test_build_summary_payload_defaults_model() -> None:
    assert summarize_client.build_summary_payload("x")["model"] == summarize_client.DEFAULT_MODEL


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"choices": [{"message": {"content": "  a summary  "}}]}, "a summary"),
        (
            {"choices": [{"message": {"content": [
                {"type": "text", "text": "part one"},
                {"type": "text", "text": "part two"},
            ]}}]},
            "part one part two",
        ),
        ({"choices": []}, ""),
        ({}, ""),
        ("nonsense", ""),
        ({"choices": [{"message": {"content": None}}]}, ""),
    ],
)
def test_extract_content(body: Any, expected: str) -> None:
    assert summarize_client._extract_content(body) == expected


def test_summarize_returns_trimmed_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(*_a: Any, **_k: Any) -> Any:
        return {"choices": [{"message": {"content": " short gist "}}]}

    monkeypatch.setattr(_loopback_http, "request", fake_request)
    assert summarize_client.summarize("http://127.0.0.1:8000", "long text") == "short gist"


def test_summarize_raises_on_empty_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_loopback_http, "request", lambda *a, **k: {"choices": []})
    with pytest.raises(summarize_client.SummarizeError):
        summarize_client.summarize("http://127.0.0.1:8000", "long text")


# --- endpoint: 200 / 400 / 404 / hub error ---------------------------------

def _seed_db(db: Path) -> dict[str, int]:
    """One chat with a long message and a short message; returns their row ids."""
    conn = store.connect(db)
    chat = store.upsert_chat(
        conn, ChatRecord(source_chat_id="c", display_name="Class 4A Group", chat_type="group")
    )
    store.insert_message(
        conn, chat,
        MessageRecord(source_message_id="m1", message_timestamp="2026-06-10T10:00:00+00:00",
                      text=_LONG, sender_label="Teacher"),
    )
    store.insert_message(
        conn, chat,
        MessageRecord(source_message_id="m2", message_timestamp="2026-06-10T10:01:00+00:00",
                      text=_SHORT, sender_label="Parent"),
    )

    def _id(src: str) -> int:
        return int(
            conn.execute(
                "SELECT id FROM messages WHERE source_message_id = ?", (src,)
            ).fetchone()["id"]
        )

    ids = {"long": _id("m1"), "short": _id("m2")}
    conn.close()
    return ids


def _app_with_db(db: Path, summarizer: Any) -> Any:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    app.state.summarizer = summarizer
    return app


def test_summarize_endpoint_returns_summary(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    ids = _seed_db(db)
    seen: dict[str, Any] = {}

    def fake_summarizer(base: str, text: str) -> str:
        seen["base"], seen["text"] = base, text
        return "Bring the signed form and 12 euros tomorrow."

    with TestClient(_app_with_db(db, fake_summarizer), client=LOOPBACK) as client:
        r = client.post(f"/api/messages/{ids['long']}/summarize")
        assert r.status_code == 200
        assert r.json() == {
            "message_id": ids["long"],
            "summary": "Bring the signed form and 12 euros tomorrow.",
        }
    # The real message text reached the summarizer over the configured hub base.
    assert seen["text"] == _LONG
    assert seen["base"].startswith("http")


def test_summarize_endpoint_rejects_short(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    ids = _seed_db(db)

    def boom(_b: str, _t: str) -> str:  # must never be called for a short message
        raise AssertionError("summarizer dialled for a too-short message")

    with TestClient(_app_with_db(db, boom), client=LOOPBACK) as client:
        assert client.post(f"/api/messages/{ids['short']}/summarize").status_code == 400


def test_summarize_endpoint_404_for_missing(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    _seed_db(db)
    with TestClient(_app_with_db(db, lambda b, t: "x"), client=LOOPBACK) as client:
        assert client.post("/api/messages/99999/summarize").status_code == 404


def test_summarize_endpoint_surfaces_hub_error(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    ids = _seed_db(db)

    def down(_b: str, _t: str) -> str:
        raise summarize_client.SummarizeError("hub unreachable", status=503)

    with TestClient(_app_with_db(db, down), client=LOOPBACK) as client:
        r = client.post(f"/api/messages/{ids['long']}/summarize")
        assert r.status_code == 503
        assert "unreachable" in r.json()["detail"]
