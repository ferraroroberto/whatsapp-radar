"""Read-only Gmail API credentials, whitelist, and sender-discovery bounds (#166)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GmailSender:
    """One explicitly allowed sender represented as a stable Gmail chat."""

    address: str
    name: str


@dataclass(frozen=True)
class GmailLabel:
    """One explicitly allowed Gmail label represented as a stable chat."""

    name: str
    display_name: str


@dataclass(frozen=True)
class GmailConfig:
    """Read-only Gmail API credentials, whitelist, and sender-discovery bounds (#166).

    ``senders``/``labels`` are the explicit whitelist (full-history ingest, as
    before). Sender-level monitoring (#166) additionally *discovers* senders active
    in the last ``discovery_days`` and ingests only that bounded window for them, so
    the mailbox never floods the store. ``retention_days`` prunes messages from
    **unmonitored** Gmail senders past that window — monitored senders are exempt and
    WhatsApp data is never touched.
    """

    credentials_path: Path = Path("auth/gmail/credentials.json")
    token_path: Path = Path("auth/gmail/token.json")
    senders: tuple[GmailSender, ...] = ()
    labels: tuple[GmailLabel, ...] = ()
    # Sender discovery: how many days back to look for active senders and the hard
    # cap on messages scanned per discovery pass (the mailbox is huge — this bounds
    # the metadata reads). A discovered, unmonitored sender's messages are ingested
    # only within this window and pruned past ``retention_days``.
    discovery_days: int = 30
    discovery_max_messages: int = 400
    # Retention window for unmonitored Gmail senders. Monitored senders are exempt.
    retention_days: int = 30


def parse(raw: dict[str, Any], root: Path) -> GmailConfig:
    credentials = Path(
        os.environ.get(
            "WR_GMAIL_CREDENTIALS_PATH",
            raw.get("credentials_path", "auth/gmail/credentials.json"),
        )
    )
    if not credentials.is_absolute():
        credentials = root / credentials
    token = Path(
        os.environ.get("WR_GMAIL_TOKEN_PATH", raw.get("token_path", "auth/gmail/token.json"))
    )
    if not token.is_absolute():
        token = root / token
    return GmailConfig(
        credentials_path=credentials,
        token_path=token,
        discovery_days=int(
            os.environ.get("WR_GMAIL_DISCOVERY_DAYS", raw.get("discovery_days", 30))
        ),
        discovery_max_messages=int(
            os.environ.get(
                "WR_GMAIL_DISCOVERY_MAX_MESSAGES", raw.get("discovery_max_messages", 400)
            )
        ),
        retention_days=int(
            os.environ.get("WR_GMAIL_RETENTION_DAYS", raw.get("retention_days", 30))
        ),
        senders=tuple(
            GmailSender(
                address=str(item.get("address", "")).strip().lower(),
                name=str(item.get("name") or item.get("address") or "").strip(),
            )
            for item in raw.get("senders", [])
            if isinstance(item, dict) and str(item.get("address", "")).strip()
        ),
        labels=tuple(
            GmailLabel(
                name=str(item.get("name", "")).strip(),
                display_name=str(item.get("display_name") or item.get("name") or "").strip(),
            )
            for item in raw.get("labels", [])
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ),
    )
