"""Stage-1-only signal scan for chats the operator has not monitored yet.

This module intentionally imports only the deterministic keyword matcher. It
does not build or call a classifier: a tripwire hit is a suggestion to monitor a
chat, never a Stage-2 classification or actionable-message verdict.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from src.analysis.keywords import matched_rules
from src.config import TripwireConfig
from src.db.tripwire import recent_discovered_messages


@dataclass(frozen=True)
class TripwireHit:
    chat_id: int
    source: str
    display_name: str
    latest_message_at: str
    matched_messages: int
    roots: tuple[str, ...]
    buckets: tuple[str, ...]


@dataclass(frozen=True)
class TripwireScan:
    hits: tuple[TripwireHit, ...]
    scanned_messages: int
    truncated: bool


@dataclass
class _HitBuilder:
    source: str
    display_name: str
    latest_message_at: str
    matched_messages: int = 0
    roots: dict[str, None] = field(default_factory=dict)
    buckets: dict[str, None] = field(default_factory=dict)


def scan_tripwire(
    conn: sqlite3.Connection,
    config: TripwireConfig,
    *,
    now: datetime | None = None,
) -> TripwireScan:
    """Scan the configured recent/capped slice and aggregate Stage-1 hits by chat."""
    current = now or datetime.now(UTC)
    cutoff = (current - timedelta(days=config.window_days)).isoformat(timespec="seconds")
    rows, truncated = recent_discovered_messages(
        conn,
        cutoff=cutoff,
        max_messages=config.max_messages,
        max_messages_per_chat=config.max_messages_per_chat,
    )

    by_chat: dict[int, _HitBuilder] = {}
    for row in rows:
        rules = matched_rules(row["text"], str(row["source"]))
        if not rules:
            continue
        chat_id = int(row["chat_id"])
        hit = by_chat.setdefault(
            chat_id,
            _HitBuilder(
                source=str(row["source"]),
                display_name=str(row["display_name"]),
                latest_message_at=str(row["message_timestamp"]),
            ),
        )
        hit.matched_messages += 1
        for rule in rules:
            hit.roots.setdefault(rule.root, None)
            hit.buckets.setdefault(rule.bucket, None)

    hits = tuple(
        TripwireHit(
            chat_id=chat_id,
            source=hit.source,
            display_name=hit.display_name,
            latest_message_at=hit.latest_message_at,
            matched_messages=hit.matched_messages,
            roots=tuple(hit.roots),
            buckets=tuple(hit.buckets),
        )
        for chat_id, hit in by_chat.items()
    )
    return TripwireScan(hits=hits, scanned_messages=len(rows), truncated=truncated)
