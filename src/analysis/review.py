"""Review-engine primitives shared by the scan pipeline.

Per-chat analysis has a single implementation: :mod:`src.analysis.pipeline`'s
traced ``scan`` loop (the ``wr review`` verb is ``scan(mode="process")``). This
module holds only the pieces the ``live``/``dry_run``/``process`` modes
share: the cursor-advance invariant
(advance ONLY after the analysis row is committed, never on a contract failure,
so a bad run safely retries the same delta), the Stage-2-offline abort, the
recent-alert prompt memory, and
the transcription hold-back gate.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from src.analysis.classifier import ClassifierUnavailable
from src.analysis.contract import AnalysisResult
from src.analysis.source_funnel import SourceFunnel, ensure_source_funnel
from src.analysis.transcription import hold_back_untranscribed
from src.config import Config
from src.db import store
from src.models import StoredMessage
from src.notify.alert import send_alert
from src.progress import Progress, emit

# Funnel/notification status recorded when a run stops because Stage 2 could not
# reach the hub. Distinct from ``"offline"`` (the *source* was unreachable) so the
# audit trail names which end of the pipeline failed.
CLASSIFIER_OFFLINE_STATUS = "classifier_offline"


def send_classifier_abort_alert(
    config: Config,
    exc: ClassifierUnavailable,
    unanalysed: list[str],
    *,
    progress: Progress | None = None,
) -> str:
    """Alert that a run stopped on an unreachable Stage-2 classifier.

    Every ``scan`` mode calls the hub in a loop over monitored chats, and a dead
    hub fails identically for each remaining one, so the run stops once. The
    alert names only ``unanalysed`` — the chats whose cursors were held and whose
    deltas the next run retries — not the chats analysed before the abort.
    Returns the alert status.
    """
    alert_status, _ = send_alert(
        config,
        f"⚠️ Family Radar: run aborted — Stage-2 classifier unreachable "
        f"({exc}). {len(unanalysed)} chat(s) were left unanalysed; their cursors "
        f"are held: {', '.join(unanalysed)}. Restore the LLM hub and re-run.",
    )
    emit(progress, f"• classifier alert: {alert_status}")
    return alert_status


_RECENT_ALERT_HEADER = (
    "Previously surfaced to the user (already alerted — do NOT raise these again "
    "unless the information is genuinely new/different, or the user must still act "
    "because a deadline is now imminent):"
)


def format_recent_alerts(items: list[sqlite3.Row]) -> str | None:
    """Render already-surfaced actionable items as a Stage-2 memory block.

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
    """Short-term alert memory for a family's Stage-2 prompt.

    Built fresh from ``analysis_items`` every run rather than the single, easily
    null-wiped ``rolling_context`` summary, so a noise delta never erases the
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
    each member keeps its own cursor. Group the delta by origin chat and
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
    """Apply the untranscribed-voice-note hold-back gate when enabled.

    Shared by every ``scan`` mode that analyses live deltas so the gate can
    only be added/changed in one place.
    """
    return hold_back_untranscribed(delta) if transcription_enabled else delta


def note_delta_funnel(
    source_funnels: dict[str, SourceFunnel], source: str, delta_len: int
) -> SourceFunnel:
    """Record a chat's delta on its source funnel; shared by every scan mode."""
    source_funnel = ensure_source_funnel(source_funnels, source)
    source_funnel.channels_with_delta += 1
    source_funnel.messages_checked += delta_len
    return source_funnel


def persist_analysis_result(
    conn: sqlite3.Connection, run_id: int, chat_id: int, result: AnalysisResult
) -> int:
    """Insert a parsed :class:`AnalysisResult`; shared by every scan mode.

    Returns the new ``analysis_items`` row id — the scan pipeline needs it to
    record a routine-prep reminder's ``calendar_event_id`` after the fact.
    """
    return store.insert_analysis_item(
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
        child=result.child,
        task_category=result.task_category,
        prep_complexity=result.prep_complexity,
    )
