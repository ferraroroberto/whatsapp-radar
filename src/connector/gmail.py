"""Thin WhatsApp Radar adapter over the portable read-only Gmail component."""

from __future__ import annotations

import logging

from gmail_readonly import (
    GmailLabel,
    GmailMailbox,
    GmailProfile,
    GmailReadClient,
    GmailReadError,
    GmailSearch,
    GmailSender,
    GmailSource,
    build_google_read_client,
)

from src.config import GmailConfig
from src.connector.base import ConnectorStatus
from src.connector.preflight import ConnectorOffline
from src.models import ChatRecord, MessageRecord

logger = logging.getLogger(__name__)

_SENDER_PREFIX = "sender:"


def build_gmail_read_client(config: GmailConfig) -> GmailReadClient:
    """Build the portable Google client from WhatsApp Radar's local config."""
    return build_google_read_client(config.token_path)


class GmailConnector:
    """Map portable Gmail sources and messages into application records."""

    def __init__(
        self,
        config: GmailConfig,
        *,
        client: GmailReadClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._mailbox: GmailMailbox | None = None
        self._sources: dict[str, GmailSource] = {}
        self._status = ConnectorStatus("gmail", False, "not connected")

    def connect(self) -> ConnectorStatus:
        try:
            if self._client is None:
                self._client = build_gmail_read_client(self._config)
            self._mailbox = GmailMailbox(self._client)
            senders = tuple(
                GmailSender(sender.address, sender.name)
                for sender in self._config.senders
            )
            labels = tuple(
                GmailLabel(label.name, label.display_name)
                for label in self._config.labels
            )
            # An empty whitelist is now valid (#166): senders are discovered from the
            # last N days instead of being pre-listed. Only resolve/validate the
            # whitelist when one is configured (a missing label still fails closed).
            if senders or labels:
                sources = self._mailbox.resolve_sources(senders=senders, labels=labels)
                self._sources = {source.source_id: source for source in sources}
            else:
                self._sources = {}
            self._status = ConnectorStatus(
                "gmail",
                True,
                f"{len(self._sources)} whitelisted chat(s) + "
                f"{self._config.discovery_days}d sender discovery",
            )
        except (GmailReadError, ValueError) as exc:
            self._status = ConnectorStatus("gmail", False, str(exc))
        except Exception as exc:
            self._status = ConnectorStatus("gmail", False, _safe_error_detail(exc))
        return self._status

    def status(self) -> ConnectorStatus:
        return self._status

    def profile(self) -> GmailProfile:
        """Return the safely maskable connected-mailbox profile."""
        return self._require_connected().profile()

    def list_chats(self) -> list[ChatRecord]:
        """Whitelisted chats plus senders discovered in the last N days (#166).

        Whitelist sources (senders + labels) keep their full-history behaviour; each
        distinct sender seen in the last ``discovery_days`` is added as an ``email``
        chat so the operator can promote it to monitored. A discovery API failure is
        non-fatal — it logs and falls back to the whitelist so a hiccup never drops
        the monitored senders' ingest.
        """
        mailbox = self._require_connected()
        records = [
            ChatRecord(
                source_chat_id=source.source_id,
                display_name=source.display_name,
                chat_type="email",
                source="gmail",
            )
            for source in self._sources.values()
        ]
        seen = set(self._sources)
        try:
            discovered = mailbox.discover_senders(
                days=self._config.discovery_days,
                limit=self._config.discovery_max_messages,
            )
        except GmailReadError as exc:
            logger.warning("⚠️ Gmail sender discovery failed (%s); using whitelist only", exc)
            discovered = ()
        for sender in discovered:
            source_id = f"{_SENDER_PREFIX}{sender.address}"
            if source_id in seen:
                continue
            seen.add(source_id)
            records.append(
                ChatRecord(
                    source_chat_id=source_id,
                    display_name=sender.display_name,
                    chat_type="email",
                    source="gmail",
                )
            )
        return records

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        mailbox = self._require_connected()
        source = self._sources.get(source_chat_id)
        if source is not None:
            # Whitelisted sender/label: full-history search (unchanged).
            search = source.search
        elif source_chat_id.startswith(_SENDER_PREFIX):
            # Discovered sender: bounded to the discovery window so the store stays
            # bounded; its messages past retention are pruned unless it is monitored.
            address = source_chat_id[len(_SENDER_PREFIX):]
            search = GmailSearch(
                query=f"from:{_escape_query(address)}",
                lookback_days=self._config.discovery_days,
            )
        else:
            raise ValueError("Gmail source chat is not whitelisted")
        try:
            emails = mailbox.messages(search)
        except GmailReadError as exc:
            self._status = ConnectorStatus("gmail", False, str(exc))
            raise ConnectorOffline(self._status) from exc
        return [
            MessageRecord(
                source_message_id=email.message_id,
                message_timestamp=email.timestamp,
                text=email.text,
                sender_label=email.sender_name or email.sender_address,
                message_type="email",
                raw={
                    "thread_id": email.thread_id,
                    "label_ids": list(email.label_ids),
                    "headers": email.headers,
                },
            )
            for email in emails
        ]

    def canonical_source_id(self, source_chat_id: str) -> str | None:
        return source_chat_id

    def stop(self) -> None:
        if self._mailbox is not None:
            self._mailbox.close()
        elif self._client is not None:
            self._client.close()
        self._client = None
        self._mailbox = None
        self._sources = {}
        self._status = ConnectorStatus("gmail", False, "stopped")

    def _require_connected(self) -> GmailMailbox:
        if not self._status.connected or self._mailbox is None:
            raise ConnectorOffline(self._status)
        return self._mailbox


def _escape_query(value: str) -> str:
    """Escape a discovered address for safe inclusion in a Gmail ``from:`` query."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _safe_error_detail(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return str(exc)
    return f"Gmail API request failed ({type(exc).__name__})"
