"""Consolidate a review run's actionable items into a single digest.

One run produces at most one digest covering all monitored chats. If no chat had
an actionable item, :attr:`Digest.has_actionable_items` is ``False`` and the
caller must not produce a notification.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DigestItem:
    chat: str
    priority: str | None
    summary: str | None
    suggested_next_action: str | None
    deadline: str | None
    confidence: float | None
    evidence_message_ids: list[str]


@dataclass(frozen=True)
class Digest:
    run_id: int
    items: list[DigestItem]

    @property
    def has_actionable_items(self) -> bool:
        return bool(self.items)

    def to_json(self) -> str:
        return json.dumps(
            {
                "run_id": self.run_id,
                "actionable_count": len(self.items),
                "items": [asdict(i) for i in self.items],
            },
            ensure_ascii=False,
            indent=2,
        )

    def to_telegram_text(self) -> str:
        """Render the digest as a plain-text message for a notification channel.

        Kept deliberately plain (no Markdown/HTML) so chat names or message text
        can never break Telegram's entity parsing or be misread as markup.
        """
        if not self.items:
            return "WhatsApp Radar: no actionable items."
        header = f"WhatsApp Radar — {len(self.items)} item(s) need attention:"
        blocks = [header]
        for i in self.items:
            lines = [f"• {i.chat}" + (f" [{i.priority}]" if i.priority else "")]
            if i.summary:
                lines.append(f"  {i.summary}")
            if i.suggested_next_action:
                lines.append(f"  → {i.suggested_next_action}")
            if i.deadline:
                lines.append(f"  ⏰ {i.deadline}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


def build_digest(conn: sqlite3.Connection, run_id: int) -> Digest:
    """Build the consolidated digest for a run from its actionable analysis items."""
    items = [
        DigestItem(
            chat=row["display_name"],
            priority=row["priority"],
            summary=row["summary"],
            suggested_next_action=row["suggested_next_action"],
            deadline=row["deadline"],
            confidence=row["confidence"],
            evidence_message_ids=json.loads(row["evidence_message_ids_json"] or "[]"),
        )
        for row in store_actionable(conn, run_id)
    ]
    return Digest(run_id=run_id, items=items)


def store_actionable(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    # Thin indirection kept local so report does not import the whole store module's
    # surface; the query lives with the schema knowledge in db.store.
    from ..db import store

    return store.actionable_items_for_run(conn, run_id)
