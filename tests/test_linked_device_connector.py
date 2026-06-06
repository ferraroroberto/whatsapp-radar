"""LinkedDeviceConnector reads the sidecar's NDJSON buffer — read-only, deduped.

Uses only sanitized generic data (no real chat names, numbers, or message text).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.connector.linked_device import LinkedDeviceConnector


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


def test_jid_variants_associate_to_one_chat(tmp_path: Path) -> None:
    # Same identity arrives device-scoped and via the legacy @c.us domain; both
    # must fold onto the canonical phone JID so neither strands its messages (#23).
    _write_buffer(
        tmp_path,
        chats=[{"jid": "44123@s.whatsapp.net", "name": "Ana", "type": "dm"}],
        messages=[
            {"jid": "44123:7@s.whatsapp.net", "msg_id": "A", "ts": "2026-06-10T10:00:00+00:00",
             "sender": "Ana", "text": "hi", "type": "text"},
            {"jid": "44123@c.us", "msg_id": "B", "ts": "2026-06-10T10:01:00+00:00",
             "sender": "Ana", "text": "again", "type": "text"},
        ],
    )
    connector = LinkedDeviceConnector(tmp_path)
    chats = {c.source_chat_id: c for c in connector.list_chats()}
    assert set(chats) == {"44123@s.whatsapp.net"}  # variants collapsed to one row
    assert chats["44123@s.whatsapp.net"].display_name == "Ana"
    msgs = connector.fetch_messages("44123@s.whatsapp.net")
    assert [m.source_message_id for m in msgs] == ["A", "B"]  # both variants associated


def test_lid_alias_folds_messages_onto_phone_chat(tmp_path: Path) -> None:
    # The sidecar emits an alias row pairing a contact's @lid form with its phone
    # JID; messages keyed under the @lid must associate to the named phone chat (#23).
    _write_buffer(
        tmp_path,
        chats=[
            {"jid": "44999@s.whatsapp.net", "name": "School Office", "type": "dm"},
            {"jid": "771122@lid", "alias_for": "44999@s.whatsapp.net"},
        ],
        messages=[
            {"jid": "771122@lid", "msg_id": "A", "ts": "2026-06-10T09:00:00+00:00",
             "sender": "School Office", "text": "term dates", "type": "text"},
        ],
    )
    connector = LinkedDeviceConnector(tmp_path)
    chats = {c.source_chat_id: c for c in connector.list_chats()}
    assert "771122@lid" not in chats  # the alias never surfaces as its own chat
    assert chats["44999@s.whatsapp.net"].display_name == "School Office"
    msgs = connector.fetch_messages("44999@s.whatsapp.net")
    assert [m.source_message_id for m in msgs] == ["A"]
    # Looking the chat up by its @lid form resolves to the same messages.
    assert [m.source_message_id for m in connector.fetch_messages("771122@lid")] == ["A"]


def test_unnamed_dm_falls_back_to_formatted_number(tmp_path: Path) -> None:
    # A DM the sidecar never labelled and that carries no push name must show a
    # readable +number, never the raw <number>@s.whatsapp.net JID (#22).
    _write_buffer(
        tmp_path,
        chats=[{"jid": "44555000@s.whatsapp.net", "name": None, "type": "dm"}],
        messages=[],
    )
    chat = LinkedDeviceConnector(tmp_path).list_chats()[0]
    assert chat.display_name == "+44555000"


def test_unnamed_dm_derives_name_from_push_name(tmp_path: Path) -> None:
    # No saved contact name, but the remote's push name on a message names the DM (#22).
    _write_buffer(
        tmp_path,
        chats=[{"jid": "44777@s.whatsapp.net", "name": None, "type": "dm"}],
        messages=[
            {"jid": "44777@s.whatsapp.net", "msg_id": "A", "ts": "2026-06-10T08:00:00+00:00",
             "sender": "me", "text": "hello?", "type": "text", "raw": {"from_me": True}},
            {"jid": "44777@s.whatsapp.net", "msg_id": "B", "ts": "2026-06-10T08:01:00+00:00",
             "sender": "Coach Pat", "text": "yes", "type": "text", "raw": {"from_me": False}},
        ],
    )
    chat = LinkedDeviceConnector(tmp_path).list_chats()[0]
    assert chat.display_name == "Coach Pat"  # remote push name, not "me" or +number


def test_sender_label_falls_back_to_participant(tmp_path: Path) -> None:
    # History-synced group messages lack a push name; the participant JID in raw
    # is humanized so the conversation overlay attributes the sender (#22).
    _write_buffer(
        tmp_path,
        chats=[{"jid": "123@g.us", "name": "Class 4A Group", "type": "group"}],
        messages=[
            {"jid": "123@g.us", "msg_id": "A", "ts": "2026-06-10T10:00:00+00:00",
             "sender": None, "text": "history line", "type": "text",
             "raw": {"from_me": False, "participant": "44321@s.whatsapp.net"}},
            {"jid": "123@g.us", "msg_id": "B", "ts": "2026-06-10T10:01:00+00:00",
             "sender": None, "text": "mine", "type": "text", "raw": {"from_me": True}},
        ],
    )
    fetched = LinkedDeviceConnector(tmp_path).fetch_messages("123@g.us")
    msgs = {m.source_message_id: m for m in fetched}
    assert msgs["A"].sender_label == "+44321"  # humanized participant
    assert msgs["B"].sender_label == "me"  # own message
