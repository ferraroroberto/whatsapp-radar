"""Gmail sender-level retention (#166): bound the store for unmonitored senders.

Sender discovery lists every Gmail sender active in the last N days, which would
grow the store without bound. Retention keeps it bounded: messages from
**unmonitored** Gmail senders older than ``retention_days`` are pruned, and a
discovered sender left with no messages is dropped from the list.

Three invariants this module guarantees (each asserted by a test):

- **Monitored senders are exempt.** A Gmail chat with ``status = 'monitored'`` is
  never pruned — its history is kept like a monitored WhatsApp chat.
- **WhatsApp is never touched.** Every statement is scoped to ``source = 'gmail'``.
- **Idempotent.** A second run over the same cutoff prunes nothing.

The prune is logged (counts per run) by the caller so a retention pass is visible
in the sync log and the process log.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class PruneOutcome:
    """What one retention pass removed from the store."""

    messages_pruned: int
    senders_removed: int
    cutoff: str

    @property
    def is_noop(self) -> bool:
        return self.messages_pruned == 0 and self.senders_removed == 0


def prune_gmail_unmonitored(
    conn: sqlite3.Connection,
    *,
    retention_days: int = 30,
    now: datetime | None = None,
) -> PruneOutcome:
    """Prune unmonitored Gmail senders' messages past ``retention_days``.

    Deletes messages older than the cutoff that belong to Gmail chats which are
    **not** monitored, then removes any now-empty unmonitored Gmail sender rows so
    the "senders seen in the last N days" list stays honest. Monitored Gmail
    senders and every WhatsApp chat are left byte-for-byte intact. The cutoff is a
    UTC-ISO string, matching stored ``message_timestamp`` values, so the ``<``
    comparison is chronological. Commits once; safe to run every sync.
    """
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=retention_days)).isoformat()
    messages_pruned = conn.execute(
        "DELETE FROM messages WHERE message_timestamp < ? AND chat_id IN ("
        "    SELECT id FROM chats WHERE source = 'gmail' AND status != 'monitored')",
        (cutoff,),
    ).rowcount
    senders_removed = conn.execute(
        "DELETE FROM chats WHERE source = 'gmail' AND status != 'monitored' "
        "AND id NOT IN (SELECT DISTINCT chat_id FROM messages)",
    ).rowcount
    conn.commit()
    return PruneOutcome(
        messages_pruned=int(messages_pruned),
        senders_removed=int(senders_removed),
        cutoff=cutoff,
    )
