"""Offline GmailConnector contract, filtering, normalization, and failures."""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from gmail_readonly import GoogleGmailReadClient

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
        # Sender discovery (#166) scans a recent window with a ``newer_than`` query;
        # these fixtures exercise the whitelist path, so discovery finds nothing here.
        if "newer_than" in query:
            return []
        return ["newer", "older"]

    def get_message(
        self,
        message_id: str,
        *,
        metadata_only: bool = False,
    ) -> dict[str, Any]:
        assert metadata_only is False
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


def test_missing_label_fails_closed_but_empty_whitelist_discovers(tmp_path: Path) -> None:
    missing = GmailConnector(
        GmailConfig(
            senders=(),
            labels=(GmailLabel("Missing", "Missing"),),
        ),
        client=_FakeClient(),
    )
    assert missing.connect().connected is False
    assert "not found" in missing.status().detail

    # An empty whitelist is now valid (#166): senders are discovered from the last
    # N days rather than pre-listed, so the connector connects in discovery mode.
    empty = GmailConnector(GmailConfig(), client=_FakeClient())
    status = empty.connect()
    assert status.connected is True
    assert "discovery" in status.detail
    assert empty.list_chats() == []


class _DiscoveryClient(_FakeClient):
    """A client whose recent window surfaces senders (metadata reads allowed)."""

    def list_message_ids(
        self,
        *,
        query: str,
        label_ids: list[str] | None = None,
    ) -> list[str]:
        self.queries.append((query, label_ids))
        return ["newer", "older"]

    def get_message(
        self,
        message_id: str,
        *,
        metadata_only: bool = False,
    ) -> dict[str, Any]:
        return self.messages[message_id]


def test_list_chats_includes_discovered_senders_deduped(tmp_path: Path) -> None:
    connector = GmailConnector(
        GmailConfig(senders=(GmailSender("school@example.com", "School notices"),)),
        client=_DiscoveryClient(),
    )
    connector.connect()

    chats = connector.list_chats()
    ids = [chat.source_chat_id for chat in chats]

    # The whitelisted sender keeps its configured name and appears once; the
    # non-whitelisted sender seen in the window (coach) is added as discovered.
    assert ids.count("sender:school@example.com") == 1
    assert "sender:coach@example.com" in ids
    school = next(c for c in chats if c.source_chat_id == "sender:school@example.com")
    assert school.display_name == "School notices"
    assert {chat.chat_type for chat in chats} == {"email"}


def test_discovered_sender_fetch_is_windowed(tmp_path: Path) -> None:
    client = _DiscoveryClient()
    connector = GmailConnector(GmailConfig(discovery_days=30), client=client)
    connector.connect()

    connector.fetch_messages("sender:coach@example.com")

    # A discovered (non-whitelisted) sender is fetched only within the window, so a
    # huge mailbox history never floods the store.
    assert ("from:coach@example.com newer_than:30d", None) in client.queries


def test_discovery_failure_falls_back_to_whitelist(tmp_path: Path) -> None:
    class _Response:
        status = 429

    class _DiscoveryFailsClient(_FakeClient):
        def list_message_ids(
            self,
            *,
            query: str,
            label_ids: list[str] | None = None,
        ) -> list[str]:
            self.queries.append((query, label_ids))
            if "newer_than" in query:
                error = RuntimeError("private discovery detail")
                error.resp = _Response()  # type: ignore[attr-defined]
                raise error
            return ["newer", "older"]

    connector = GmailConnector(
        GmailConfig(senders=(GmailSender("school@example.com", "School"),)),
        client=_DiscoveryFailsClient(),
    )
    connector.connect()

    # A discovery hiccup must not drop the whitelisted (monitored) sender's ingest.
    chats = connector.list_chats()
    assert [chat.source_chat_id for chat in chats] == ["sender:school@example.com"]
    assert connector.status().connected is True


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


def test_discovered_ingest_is_single_pass(tmp_path: Path) -> None:
    client = _DiscoveryClient()
    connector = GmailConnector(GmailConfig(discovery_days=30), client=client)
    connector.connect()

    chats = connector.list_chats()
    for chat in chats:
        connector.fetch_messages(chat.source_chat_id)

    # One windowed retrieval serves discovery AND every discovered sender's
    # messages — no per-sender API searches (#180: the old shape cost one
    # search per discovered sender on every sync).
    assert len(chats) == 2
    assert len(client.queries) == 1
    assert "newer_than:30d" in client.queries[0][0]


def test_second_sync_downloads_no_bodies(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    class _CountingClient(_DiscoveryClient):
        def __init__(self) -> None:
            super().__init__()
            self.full_gets = 0

        def get_message(
            self,
            message_id: str,
            *,
            metadata_only: bool = False,
        ) -> dict[str, Any]:
            if not metadata_only:
                self.full_gets += 1
            return self.messages[message_id]

    first_client = _CountingClient()
    sync_sources(
        conn,
        [ConnectorBinding("gmail", GmailConnector(GmailConfig(), client=first_client))],
        operation="ingest",
    )
    second_client = _CountingClient()
    outcome = sync_sources(
        conn,
        [ConnectorBinding("gmail", GmailConnector(GmailConfig(), client=second_client))],
        operation="ingest",
    )

    # First run downloads the window's bodies; a re-sync over an unchanged
    # mailbox diffs ids against the store and downloads nothing (#180).
    assert first_client.full_gets == 2
    assert second_client.full_gets == 0
    assert outcome.delta.messages_added == 0
