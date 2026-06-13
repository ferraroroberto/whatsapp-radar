"""Review engine — the heart of the spike.

For each monitored chat it selects only the message delta (since the cursor),
classifies it, validates the JSON, persists the analysis, and *then* advances the
cursor. The cursor is advanced ONLY after the analysis row is committed, and not
at all when the classifier output fails the contract — so a bad run is safely
retried against the same delta next time.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from src.analysis.classifier import Classifier
from src.analysis.contract import ContractError, parse_analysis
from src.db import store
from src.models import StoredMessage


@dataclass
class ReviewOutcome:
    run_id: int
    chats_with_delta: int = 0
    messages_processed: int = 0
    actionable_chats: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)


_RECENT_ALERT_HEADER = (
    "Previously surfaced to the user (already alerted — do NOT raise these again "
    "unless the information is genuinely new/different, or the user must still act "
    "because a deadline is now imminent):"
)


def format_recent_alerts(items: list[sqlite3.Row]) -> str | None:
    """Render already-surfaced actionable items as a Stage-2 memory block (#66).

    Returns ``None`` for an empty window so the prompt simply omits the section.
    Each line carries the run date and the stated deadline (when present) so the
    model can both suppress a stale repeat and escalate one whose deadline is now
    imminent.
    """
    if not items:
        return None
    lines = [_RECENT_ALERT_HEADER]
    for it in items:
        day = (it["started_at"] or "")[:10]
        entry = f"- [{day}] {it['summary']}"
        if it["deadline"]:
            entry += f" — deadline: {it['deadline']}"
        lines.append(entry)
    return "\n".join(lines)


def recent_alert_context(
    conn: sqlite3.Connection,
    head_id: int,
    *,
    since_days: int,
    now: datetime | None = None,
    exclude_run_id: int | None = None,
) -> str | None:
    """Short-term alert memory for a family's Stage-2 prompt (#66).

    Built fresh from ``analysis_items`` every run rather than the single, easily
    null-wiped ``rolling_context`` summary, so a noise delta no longer erases the
    memory of a still-relevant earlier alert.
    """
    return format_recent_alerts(
        store.recent_actionable_items(
            conn, head_id, since_days=since_days, now=now, exclude_run_id=exclude_run_id
        )
    )


def advance_family_cursors(
    conn: sqlite3.Connection,
    head_id: int,
    delta: list[StoredMessage],
    summary: str | None,
) -> None:
    """Advance each consumed member's cursor after the family analysis is persisted.

    A family review folds the head and its linked children into one merged delta;
    each member keeps its own cursor (#37). Group the delta by origin chat and
    advance every member by *its own* max ingestion id — not ``delta[-1]`` — so a
    backfilled out-of-send-order message is never skipped next run. Only the head
    carries the rolling summary; folded children, which are never reviewed
    standalone, advance with no rolling context of their own.
    """
    by_chat: dict[int, list[StoredMessage]] = {}
    for m in delta:
        by_chat.setdefault(m.chat_id, []).append(m)
    for cid, msgs in by_chat.items():
        last = max(msgs, key=lambda m: m.id)
        rolling = (
            json.dumps(
                {"last_summary": summary, "last_message_id": last.source_message_id}
            )
            if cid == head_id
            else None
        )
        store.advance_cursor(conn, cid, last.id, last.message_timestamp, rolling)


def review_monitored_chats(
    conn: sqlite3.Connection, classifier: Classifier, *, since_days: int = 7
) -> ReviewOutcome:
    """Review every monitored family's delta and persist results within one run.

    Iterates *family heads* (``store.monitored_chats`` already excludes linked
    children); each head's delta is the merge of its own and its children's
    messages since each member's cursor, classified once under the head.
    """
    run_id = store.start_run(conn)
    outcome = ReviewOutcome(run_id=run_id)

    for chat in store.monitored_chats(conn):
        chat_id = int(chat["id"])
        delta = store.family_delta_since_cursor(conn, chat_id)
        if not delta:
            continue

        outcome.chats_with_delta += 1
        prior = recent_alert_context(
            conn, chat_id, since_days=since_days, exclude_run_id=run_id
        )

        try:
            raw = classifier.classify(chat["display_name"], delta, prior)
            result = parse_analysis(raw)
        except ContractError as exc:
            # Do NOT advance the cursor: the same delta is retried next run.
            outcome.errors.append((chat_id, str(exc)))
            continue

        store.insert_analysis_item(
            conn,
            run_id,
            chat_id,
            action_required=result.action_required,
            priority=result.priority,
            summary=result.summary,
            suggested_next_action=result.suggested_next_action,
            deadline=result.deadline,
            deadline_date=result.deadline_date,
            confidence=result.confidence,
            evidence_message_ids_json=json.dumps(result.evidence_message_ids),
        )

        outcome.messages_processed += len(delta)
        if result.action_required:
            outcome.actionable_chats += 1

        # Cursor advance happens only after the analysis row above is committed,
        # and per-member across the family (each member keeps its own cursor).
        advance_family_cursors(conn, chat_id, delta, result.summary)

    status = "completed_with_errors" if outcome.errors else "completed"
    store.finish_run(conn, run_id, status, outcome.chats_with_delta)
    return outcome
