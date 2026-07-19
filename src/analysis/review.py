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
from src.analysis.contract import AnalysisResult, ContractError, parse_analysis
from src.analysis.keywords import has_actionable_signal
from src.analysis.source_funnel import (
    SourceFunnel,
    ensure_source_funnel,
    source_funnels_json,
)
from src.analysis.transcription import hold_back_untranscribed
from src.config import Config
from src.db import store
from src.models import StoredMessage


@dataclass
class ReviewOutcome:
    run_id: int
    chats_with_delta: int = 0
    messages_processed: int = 0
    actionable_chats: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    source_funnels: dict[str, SourceFunnel] = field(default_factory=dict)


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


def hold_back_if_transcribing(
    delta: list[StoredMessage], transcription_enabled: bool
) -> list[StoredMessage]:
    """Apply the untranscribed-voice-note hold-back gate (#36/#132) when enabled.

    Shared by ``scan``'s live loop and ``review_monitored_chats`` so the gate
    can only be added/changed in one place (issue #129).
    """
    return hold_back_untranscribed(delta) if transcription_enabled else delta


def note_delta_funnel(
    source_funnels: dict[str, SourceFunnel], source: str, delta_len: int
) -> SourceFunnel:
    """Record a chat's delta on its source funnel; shared by scan and review."""
    source_funnel = ensure_source_funnel(source_funnels, source)
    source_funnel.channels_with_delta += 1
    source_funnel.messages_checked += delta_len
    return source_funnel


def persist_analysis_result(
    conn: sqlite3.Connection, run_id: int, chat_id: int, result: AnalysisResult
) -> None:
    """Insert a parsed :class:`AnalysisResult`; shared by scan and review."""
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


def review_monitored_chats(
    conn: sqlite3.Connection,
    classifier: Classifier,
    *,
    since_days: int = 7,
    config: Config | None = None,
) -> ReviewOutcome:
    """Review every monitored family's delta and persist results within one run.

    Iterates *family heads* (``store.monitored_chats`` already excludes linked
    children); each head's delta is the merge of its own and its children's
    messages since each member's cursor, classified once under the head.

    Holds the delta before any voice note still awaiting transcription (#132),
    mirroring ``scan``'s live-mode gate: without it, a note stuck ``pending``/
    ``failed`` gets classified on its literal "[voice note]" placeholder and the
    family cursor advances past it, so the real transcript is never analysed.
    Gated on ``config.transcription.enabled``; ``config=None`` (as in tests with
    no transcription feature in play) leaves the delta untouched.
    """
    run_id = store.start_run(conn, kind="process")
    outcome = ReviewOutcome(run_id=run_id)
    monitored = store.monitored_chats(conn)
    if config is not None:
        for source in config.sources:
            ensure_source_funnel(outcome.source_funnels, source)
    for chat in monitored:
        ensure_source_funnel(
            outcome.source_funnels, str(chat["source"])
        ).monitored_channels += 1

    for chat in monitored:
        chat_id = int(chat["id"])
        source = str(chat["source"])
        delta = store.family_delta_since_cursor(conn, chat_id)
        delta = hold_back_if_transcribing(
            delta, config is not None and config.transcription.enabled
        )
        if not delta:
            continue

        outcome.chats_with_delta += 1
        source_funnel = note_delta_funnel(outcome.source_funnels, source, len(delta))
        signal = (
            has_actionable_signal(delta, source)
            if config is not None and config.classifier == "cascade"
            else None
        )
        if signal is not None:
            if signal.matched:
                source_funnel.stage1_passed += 1
            else:
                source_funnel.stage1_rejected += 1
        if config is not None and (
            config.classifier == "hub"
            or (config.classifier == "cascade" and signal is not None and signal.matched)
        ):
            source_funnel.llm_calls += 1
        prior = recent_alert_context(
            conn, chat_id, since_days=since_days, exclude_run_id=run_id
        )

        try:
            raw = classifier.classify(
                chat["display_name"], delta, prior, source=source
            )
            result = parse_analysis(raw)
        except ContractError as exc:
            # Do NOT advance the cursor: the same delta is retried next run.
            outcome.errors.append((chat_id, str(exc)))
            continue

        persist_analysis_result(conn, run_id, chat_id, result)

        outcome.messages_processed += len(delta)
        if result.action_required:
            outcome.actionable_chats += 1
            source_funnel.actionable += 1

        # Cursor advance happens only after the analysis row above is committed,
        # and per-member across the family (each member keeps its own cursor).
        advance_family_cursors(conn, chat_id, delta, result.summary)
        source_funnel.cursors_advanced += 1

    status = "completed_with_errors" if outcome.errors else "completed"
    store.finish_run(conn, run_id, status, outcome.chats_with_delta)
    store.record_run_funnel(
        conn,
        run_id,
        chats_synced=0,
        messages_synced=0,
        chats_monitored=len(monitored),
        stage1_passed=sum(f.stage1_passed for f in outcome.source_funnels.values()),
        stage2_llm_calls=sum(f.llm_calls for f in outcome.source_funnels.values()),
        actionable=outcome.actionable_chats,
        notification_status="none",
        source_funnel_json=source_funnels_json(outcome.source_funnels),
    )
    return outcome
