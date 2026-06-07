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


def prior_context(rolling_context_json: str | None) -> str | None:
    """Extract the last rolling summary from a chat's stored context JSON, if any."""
    if not rolling_context_json:
        return None
    try:
        value = json.loads(rolling_context_json).get("last_summary")
    except (json.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, str) else None


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


def review_monitored_chats(conn: sqlite3.Connection, classifier: Classifier) -> ReviewOutcome:
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
        prior = prior_context(store.get_rolling_context(conn, chat_id))

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
