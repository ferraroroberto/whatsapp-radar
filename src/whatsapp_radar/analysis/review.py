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

from ..db import store
from .classifier import Classifier
from .contract import ContractError, parse_analysis


@dataclass
class ReviewOutcome:
    run_id: int
    chats_with_delta: int = 0
    messages_processed: int = 0
    actionable_chats: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)


def _prior_context(rolling_context_json: str | None) -> str | None:
    if not rolling_context_json:
        return None
    try:
        value = json.loads(rolling_context_json).get("last_summary")
    except (json.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def review_monitored_chats(conn: sqlite3.Connection, classifier: Classifier) -> ReviewOutcome:
    """Review every monitored chat's delta and persist results within one run."""
    run_id = store.start_run(conn)
    outcome = ReviewOutcome(run_id=run_id)

    for chat in store.monitored_chats(conn):
        chat_id = int(chat["id"])
        delta = store.messages_since_cursor(conn, chat_id)
        if not delta:
            continue

        outcome.chats_with_delta += 1
        prior = _prior_context(store.get_rolling_context(conn, chat_id))

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

        last = delta[-1]
        rolling = json.dumps(
            {"last_summary": result.summary, "last_message_id": last.source_message_id}
        )
        # Cursor advance happens only after the analysis row above is committed.
        store.advance_cursor(conn, chat_id, last.id, last.message_timestamp, rolling)

    status = "completed_with_errors" if outcome.errors else "completed"
    store.finish_run(conn, run_id, status, outcome.chats_with_delta)
    return outcome
