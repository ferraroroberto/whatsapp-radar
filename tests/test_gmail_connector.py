"""Offline GmailConnector contract, filtering, normalization, and failures."""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from src.config import (
    Config,
    GmailConfig,
    GmailLabel,
    GmailSender,
    HubConfig,
    TelegramConfig,
)
from src.connector.factory import ConnectorBinding, build_connectors
from src.connector.gmail import (
    GmailConnector,
    GoogleGmailReadClient,
    build_gmail_read_client,
)
from src.connector.preflight import ConnectorOffline
from src.db import store
from src.db.sync import sync_sources


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _gmail_config(tmp_path: Path) -> GmailConfig:
    return GmailConfig(
        credentials_path=tmp_path / "credentials.json",
        token_path=tmp_path / "token.json",
        senders=(GmailSender("school@example.com", "School notices"),),
        labels=(
            GmailLabel("Family/Activities", "Activity mail"),
            GmailLabel("Family/Urgent", "Urgent mail"),
        ),
    )


class _FakeClient:
    def __init__(self) -> None:
        self.queries: list[tuple[str, list[str] | None]] = []
        self.closed = False
        self.messages: dict[str, dict[str, Any]] = {
            "newer": {
                "id": "newer",
                "threadId": "thread-2",
                "internalDate": "1783681200000",
                "labelIds": ["LBL_ACT"],
                "payload": {
                    "mimeType": "multipart/mixed",
                    "headers": [
                        {"name": "From", "value": "School Office <school@example.com>"},
                        {"name": "To", "value": "family@example.net"},
                        {"name": "Subject", "value": "Trip form"},
                        {"name": "Message-ID", "value": "<newer@example.com>"},
                        {"name": "References", "value": "<older@example.com>"},
                    ],
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "filename": "",
                            "body": {"data": _encoded("Return by Friday.")},
                        },
                        {
                            "mimeType": "application/pdf",
                            "filename": "private.pdf",
                            "body": {"data": _encoded("must never be decoded")},
                        },
                    ],
                },
            },
            "older": {
                "id": "older",
                "threadId": "thread-1",
                "internalDate": "1783594800000",
                "payload": {
                    "mimeType": "text/html",
                    "headers": [
                        {"name": "From", "value": "Coach <coach@example.com>"},
                        {"name": "Subject", "value": "Practice"},
                    ],
                    "body": {"data": _encoded("<p>Bring <strong>water</strong>.</p>")},
                },
            },
        }

    def list_labels(self) -> list[dict[str, Any]]:
        return [
            {"id": "LBL_ACT", "name": "Family/Activities"},
            {"id": "LBL_URG", "name": "Family/Urgent"},
        ]

    def list_message_ids(
        self,
        *,
        query: str,
        label_ids: list[str] | None = None,
    ) -> list[str]:
        self.queries.append((query, label_ids))
        return ["newer", "older"]

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self.messages[message_id]

    def close(self) -> None:
        self.closed = True


def test_connect_and_list_whitelisted_chats(tmp_path: Path) -> None:
    connector = GmailConnector(_gmail_config(tmp_path), client=_FakeClient())

    status = connector.connect()
    chats = connector.list_chats()

    assert status.connected is True
    assert [(chat.source_chat_id, chat.display_name, chat.source) for chat in chats] == [
        ("sender:school@example.com", "School notices", "gmail"),
        ("label:LBL_ACT", "Activity mail", "gmail"),
        ("label:LBL_URG", "Urgent mail", "gmail"),
    ]
    assert {chat.chat_type for chat in chats} == {"email"}


def test_sender_fetch_normalizes_oldest_first_and_ignores_attachment(tmp_path: Path) -> None:
    client = _FakeClient()
    connector = GmailConnector(_gmail_config(tmp_path), client=client)
    connector.connect()

    messages = connector.fetch_messages("sender:school@example.com")

    assert client.queries == [("from:school@example.com", None)]
    assert [message.source_message_id for message in messages] == ["older", "newer"]
    assert messages[0].text == "Subject: Practice\n\nBring water."
    assert messages[1].text == "Subject: Trip form\n\nReturn by Friday."
    assert "must never be decoded" not in str(messages)
    assert messages[1].message_type == "email"
    assert messages[1].sender_label == "School Office"
    assert messages[1].raw["thread_id"] == "thread-2"
    assert messages[1].raw["headers"]["message-id"] == "<newer@example.com>"


def test_label_queries_enforce_sender_then_label_precedence(tmp_path: Path) -> None:
    client = _FakeClient()
    connector = GmailConnector(_gmail_config(tmp_path), client=client)
    connector.connect()

    connector.fetch_messages("label:LBL_ACT")
    connector.fetch_messages("label:LBL_URG")

    assert client.queries[0] == ("-from:school@example.com", ["LBL_ACT"])
    assert client.queries[1] == (
        '-from:school@example.com -label:"Family/Activities"',
        ["LBL_URG"],
    )


def test_missing_label_and_empty_whitelist_fail_closed(tmp_path: Path) -> None:
    missing = GmailConnector(
        GmailConfig(
            senders=(),
            labels=(GmailLabel("Missing", "Missing"),),
        ),
        client=_FakeClient(),
    )
    assert missing.connect().connected is False
    assert "not found" in missing.status().detail

    empty = GmailConnector(GmailConfig(), client=_FakeClient())
    assert empty.connect().connected is False
    assert "whitelist is empty" in empty.status().detail


def test_missing_token_degrades_without_starting_oauth(tmp_path: Path) -> None:
    config = GmailConfig(
        token_path=tmp_path / "missing-token.json",
        senders=(GmailSender("school@example.com", "School"),),
    )
    connector = GmailConnector(config)

    status = connector.connect()

    assert status.connected is False
    assert "OAuth token missing" in status.detail
    with pytest.raises(FileNotFoundError):
        build_gmail_read_client(config)


def test_api_failure_degrades_without_leaking_detail(tmp_path: Path) -> None:
    class _Response:
        status = 429

    class _QuotaClient(_FakeClient):
        def list_message_ids(
            self,
            *,
            query: str,
            label_ids: list[str] | None = None,
        ) -> list[str]:
            error = RuntimeError("private query and message content")
            error.resp = _Response()  # type: ignore[attr-defined]
            raise error

    connector = GmailConnector(_gmail_config(tmp_path), client=_QuotaClient())
    connector.connect()

    with pytest.raises(ConnectorOffline, match="quota exceeded"):
        connector.fetch_messages("sender:school@example.com")
    assert connector.status().connected is False
    assert "private" not in connector.status().detail


def test_stop_releases_client_and_canonical_id_is_identity(tmp_path: Path) -> None:
    client = _FakeClient()
    connector = GmailConnector(_gmail_config(tmp_path), client=client)
    connector.connect()
    assert connector.canonical_source_id("sender:school@example.com") == (
        "sender:school@example.com"
    )
    connector.stop()
    assert client.closed is True
    assert connector.status().detail == "stopped"


def test_public_read_surfaces_expose_no_write_operations() -> None:
    forbidden = {
        "send",
        "draft",
        "modify",
        "archive",
        "trash",
        "delete",
        "insert",
        "label",
    }
    assert forbidden.isdisjoint(set(dir(GmailConnector)))
    assert forbidden.isdisjoint(set(dir(GoogleGmailReadClient)))


def test_factory_registers_gmail_source(tmp_path: Path) -> None:
    config = Config(
        db_path=tmp_path / "db.sqlite3",
        connector="fixture",
        classifier="stub",
        hub=HubConfig("http://127.0.0.1:8000", "stub"),
        notifier="none",
        telegram=TelegramConfig("", ""),
        linked_device_dir=tmp_path / "linked",
        sources=("whatsapp", "gmail"),
        gmail=_gmail_config(tmp_path),
    )

    bindings = build_connectors(config)

    assert [binding.source for binding in bindings] == ["whatsapp", "gmail"]
    assert isinstance(bindings[1].connector, GmailConnector)


def test_gmail_sync_is_idempotent(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    gmail = GmailConfig(
        senders=(GmailSender("school@example.com", "School"),),
    )
    first = sync_sources(
        conn,
        [ConnectorBinding("gmail", GmailConnector(gmail, client=_FakeClient()))],
        operation="ingest",
    )
    second = sync_sources(
        conn,
        [ConnectorBinding("gmail", GmailConnector(gmail, client=_FakeClient()))],
        operation="ingest",
    )

    assert first.delta.chats_added == 1
    assert first.delta.messages_added == 2
    assert second.delta.chats_added == 0
    assert second.delta.messages_added == 0
    chat_id = store.chat_id_for_source(
        conn,
        "sender:school@example.com",
        source="gmail",
    )
    assert chat_id is not None
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()["n"]
    assert count == 2
