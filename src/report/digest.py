"""Consolidate a review run's actionable items into a single digest.

One run produces at most one digest covering all monitored chats. If no chat had
an actionable item, :attr:`Digest.has_actionable_items` is ``False`` and the
caller must not produce a notification.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date


@dataclass(frozen=True)
class DigestItem:
    chat: str
    priority: str | None
    summary: str | None
    suggested_next_action: str | None
    deadline: str | None
    confidence: float | None
    evidence_message_ids: list[str]
    # The model-resolved absolute date 'YYYY-MM-DD' (#71), or None. Rendered with a
    # deterministic today/overdue flag so the human never re-interprets a relative
    # word at reading time. Defaulted so existing call sites stay backward-compatible.
    deadline_date: str | None = None


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
        return "\n\n".join([header, *(render_item(i) for i in self.items)])


def _render_deadline_line(item: DigestItem, today: date) -> str | None:
    """The ``⏰`` line for an item, preferring the resolved absolute date (#71).

    When ``deadline_date`` parses as an ISO date, render it with a deterministic
    today/overdue/tomorrow/in-N-days flag computed against ``today`` — so a stale
    "tomorrow" that now means today reads as TODAY, not a comfortable future day.
    Falls back to the free-text ``deadline`` (backward-compatible) when no resolved
    date is present, and to the raw resolved string if it didn't parse.
    """
    if item.deadline_date:
        try:
            resolved = date.fromisoformat(item.deadline_date)
        except ValueError:
            resolved = None
        if resolved is not None:
            days = (resolved - today).days
            if days < 0:
                rel = "OVERDUE"
            elif days == 0:
                rel = "TODAY"
            elif days == 1:
                rel = "tomorrow"
            else:
                rel = f"in {days} days"
            return f"  ⏰ {resolved.isoformat()} ({rel})"
    if item.deadline:
        return f"  ⏰ {item.deadline}"
    if item.deadline_date:  # present but unparseable — show it rather than drop it
        return f"  ⏰ {item.deadline_date}"
    return None


def render_item(item: DigestItem, *, today: date | None = None) -> str:
    """Render one digest item as a plain-text block (one chat's contribution).

    Shared by :meth:`Digest.to_telegram_text` and the audit trace so the per-chat
    ``telegram_text`` recorded for a run matches exactly what the digest renders.
    ``today`` (the date the resolved deadline is measured against) defaults to the
    current local date; it is injectable so the today/overdue flag is testable.
    """
    today = today or date.today()
    lines = [f"• {item.chat}" + (f" [{item.priority}]" if item.priority else "")]
    if item.summary:
        lines.append(f"  {item.summary}")
    if item.suggested_next_action:
        lines.append(f"  → {item.suggested_next_action}")
    deadline_line = _render_deadline_line(item, today)
    if deadline_line:
        lines.append(deadline_line)
    return "\n".join(lines)


def build_digest(conn: sqlite3.Connection, run_id: int) -> Digest:
    """Build the consolidated digest for a run from its actionable analysis items."""
    items = [
        DigestItem(
            chat=row["display_name"],
            priority=row["priority"],
            summary=row["summary"],
            suggested_next_action=row["suggested_next_action"],
            deadline=row["deadline"],
            deadline_date=row["deadline_date"],
            confidence=row["confidence"],
            evidence_message_ids=json.loads(row["evidence_message_ids_json"] or "[]"),
        )
        for row in store_actionable(conn, run_id)
    ]
    return Digest(run_id=run_id, items=items)


def store_actionable(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    # Thin indirection kept local so report does not import the whole store module's
    # surface; the query lives with the schema knowledge in db.store.
    from src.db import store

    return store.actionable_items_for_run(conn, run_id)
