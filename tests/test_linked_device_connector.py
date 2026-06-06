"""LinkedDeviceConnector reads the sidecar's NDJSON buffer — read-only, deduped.

Uses only sanitized generic data (no real chat names, numbers, or message text).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from whatsapp_radar.connector.linked_device import LinkedDeviceConnector


def _write_buffer(
    buffer_dir: Path,
    *,
    chats: list[dict],
    messages: list[dict],
    status: dict | None = None,
) -> None:
    buffer_dir.mkdir(parents=True, exist_ok=True)
    (buffer_dir / "chats.ndjson").write_text(
        "".join(json.dumps(c) + "\n" for c in chats), encoding="utf-8"
    )
    (buffer_dir / "messages.ndjson").write_text(
        "".join(json.dumps(m) + "\n" for m in messages), encoding="utf-8"
    )
    if status is not None:
        (buffer_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")


def test_connector_is_read_only() -> None:
    connector = LinkedDeviceConnector(Path("."))
    for forbidden in ("send", "send_message", "react", "mark_read", "delete"):
        assert not hasattr(connector, forbidden)


def test_status_not_started(tmp_path: Path) -> None:
    status = LinkedDeviceConnector(tmp_path).status()
    assert status.connected is False
    assert "sidecar not started" in status.detail


def test_status_fresh_and_stale(tmp_path: Path) -> None:
    fresh = {
        "paired": True,
        "connected": True,
        "last_update": datetime.now(UTC).isoformat(),
        "chats": 2,
        "messages": 5,
    }
    _write_buffer(tmp_path, chats=[], messages=[], status=fresh)
    assert LinkedDeviceConnector(tmp_path).status().connected is True

    stale = dict(fresh, last_update=(datetime.now(UTC) - timedelta(minutes=10)).isoformat())
    _write_buffer(tmp_path, chats=[], messages=[], status=stale)
    result = LinkedDeviceConnector(tmp_path).status()
    assert result.connected is False
    assert "stale" in result.detail


def test_list_chats_last_write_wins(tmp_path: Path) -> None:
    _write_buffer(
        tmp_path,
        chats=[
            {"jid": "123@g.us", "name": "Class 4A Group", "type": "group"},
            {"jid": "123@g.us", "name": None, "type": "group"},  # later event without a name
            {"jid": "456@s.whatsapp.net", "name": "Ana", "type": "dm"},
        ],
        messages=[],
    )
    chats = {c.source_chat_id: c for c in LinkedDeviceConnector(tmp_path).list_chats()}
    assert chats["123@g.us"].display_name == "Class 4A Group"  # name retained
    assert chats["123@g.us"].chat_type == "group"
    assert chats["456@s.whatsapp.net"].chat_type == "dm"


def test_fetch_messages_dedupes_and_orders(tmp_path: Path) -> None:
    _write_buffer(
        tmp_path,
        chats=[{"jid": "123@g.us", "name": "Class 4A Group", "type": "group"}],
        messages=[
            {"jid": "123@g.us", "msg_id": "B", "ts": "2026-06-10T10:01:00+00:00",
             "sender": "Parent", "text": "second", "type": "text"},
            {"jid": "123@g.us", "msg_id": "A", "ts": "2026-06-10T10:00:00+00:00",
             "sender": "Parent", "text": "first", "type": "text"},
            {"jid": "123@g.us", "msg_id": "A", "ts": "2026-06-10T10:00:00+00:00",
             "sender": "Parent", "text": "first (edited)", "type": "edited"},
            {"jid": "999@g.us", "msg_id": "Z", "ts": "2026-06-10T10:02:00+00:00",
             "sender": "Other", "text": "other chat", "type": "text"},
        ],
    )
    messages = LinkedDeviceConnector(tmp_path).fetch_messages("123@g.us")
    assert [m.source_message_id for m in messages] == ["A", "B"]  # ordered, other chat excluded
    assert messages[0].text == "first (edited)"  # last write wins
    assert messages[0].message_type == "edited"


def test_fetch_messages_missing_chat_is_empty(tmp_path: Path) -> None:
    _write_buffer(tmp_path, chats=[], messages=[])
    assert LinkedDeviceConnector(tmp_path).fetch_messages("nope@g.us") == []
