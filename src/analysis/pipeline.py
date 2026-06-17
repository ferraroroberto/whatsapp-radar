"""Unified scan pipeline — the single callable App Launcher's Jobs tab fires.

``scan`` collapses the ingest -> Stage 1 (keyword prefilter) -> Stage 2 (LLM) ->
digest -> deliver flow into one process, and writes a full per-run audit trace so
every decision is inspectable. A missed important message is a real failure, so
for any run we can answer: what was synced, what passed the keyword stage, what
prompt went to the LLM, what it returned, and what was delivered.

Two modes:

- **live:** sync *all* chats from the connector (monitored and not), then analyze
  only monitored deltas since each cursor, deliver one digest, and advance each
  cursor ONLY after its analysis + trace are persisted — preserving the
  read-only, retry-safe guarantee of the review engine.
- **dry_run:** no connector; replay messages already in SQLite (optionally
  windowed to the last ``days``), ignoring the cursor. Advance no cursor and
  deliver nothing, but still build the would-be digest and record the full trace.

The pipeline owns Stage 1 itself and calls a Stage-2 :class:`TracedClassifier`
(stub offline by default; the hub when configured), so the funnel and the trace
are complete regardless of which classifier runs.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from src.analysis.classifier import (
    ClassificationOutcome,
    TracedClassifier,
    build_stage2_classifier,
)
from src.analysis.contract import AnalysisResult, ContractError, parse_analysis
from src.analysis.keywords import KeywordSignal, has_actionable_signal, matched_roots
from src.analysis.review import advance_family_cursors, recent_alert_context
from src.analysis.transcription import hold_back_untranscribed, run_transcription_phase
from src.config import Config
from src.connector.base import MessageConnector
from src.connector.factory import build_connector
from src.connector.preflight import ConnectorOffline, preflight
from src.db import store
from src.models import StoredMessage
from src.notify.alert import send_alert
from src.notify.delivery import deliver_digest
from src.report.digest import Digest, DigestItem, build_digest, render_item

Mode = Literal["live", "dry_run"]

# A sink for human-readable progress lines. The CLI wires it to stdout so a
# launched run streams its funnel as it happens; tests/library callers may omit
# it. Kept deliberately string-in/None-out so it can't affect control flow.
Progress = Callable[[str], None]


def _emit(progress: Progress | None, line: str) -> None:
    if progress is not None:
        progress(line)

_NOT_ACTIONABLE = AnalysisResult(
    action_required=False,
    priority=None,
    summary=None,
    suggested_next_action=None,
    deadline=None,
    confidence=None,
    evidence_message_ids=[],
)


@dataclass
class ScanOutcome:
    """The funnel of one scan run, mirroring the persisted ``review_runs`` counters."""

    run_id: int
    mode: Mode
    chats_synced: int = 0
    messages_synced: int = 0
    chats_monitored: int = 0
    chats_with_delta: int = 0
    transcriptions: int = 0
    stage1_passed: int = 0
    stage2_llm_calls: int = 0
    actionable: int = 0
    notification_status: str = "none"
    errors: list[tuple[int, str]] = field(default_factory=list)
    digest: Digest | None = None


def _render_delta(delta: list[StoredMessage]) -> str:
    return "\n".join(
        f"[{m.source_message_id}] {m.sender_label or 'unknown'}: {m.text or ''}" for m in delta
    )


def _messages_record(delta: list[StoredMessage]) -> str:
    """Per-message audit record: each message with the Stage-1 roots it matched.

    Captured at run time (not recomputed on read) so the trace stays faithful to
    the keyword roots that actually ran, even if ``keyword_roots.txt`` changes
    later. The LLM's per-message verdict is *not* stored here — it is derived on
    read from the parsed result's ``evidence_message_ids`` (issue #12).
    """
    return json.dumps(
        [
            {
                "id": m.source_message_id,
                "sender": m.sender_label,
                "text": m.text,
                "type": m.message_type,
                "roots": matched_roots(m.text),
            }
            for m in delta
        ],
        ensure_ascii=False,
    )


def _result_json(result: AnalysisResult) -> str:
    return json.dumps(asdict(result), ensure_ascii=False)


def _write_trace(
    conn: sqlite3.Connection,
    run_id: int,
    chat_id: int,
    *,
    signal: KeywordSignal,
    delta: list[StoredMessage],
    outcome: ClassificationOutcome | None,
    result: AnalysisResult | None,
    final_action: str,
    telegram_text: str | None,
    error: str | None,
) -> None:
    store.insert_analysis_trace(
        conn,
        run_id,
        chat_id,
        input_message_ids_json=json.dumps([m.source_message_id for m in delta]),
        input_text=_render_delta(delta),
        messages_json=_messages_record(delta),
        stage1_passed=signal.matched,
        stage1_roots_json=json.dumps(list(signal.roots)),
        llm_called=outcome.llm_called if outcome else False,
        llm_system_prompt=outcome.system_prompt if outcome else None,
        llm_user_prompt=outcome.user_prompt if outcome else None,
        llm_raw_response=outcome.raw_response if outcome else None,
        parsed_result_json=_result_json(result) if result else None,
        final_action=final_action,
        telegram_text=telegram_text,
        error=error,
    )


def _advance(
    conn: sqlite3.Connection,
    mode: Mode,
    chat_id: int,
    delta: list[StoredMessage],
    summary: str | None,
) -> None:
    """Advance the family's per-member cursors (live mode only) after persistence.

    The merged delta spans the head and any linked children; each keeps its own
    ingestion-id cursor (#37), so advancement is grouped by origin chat rather
    than a single high-water mark. Dry-run advances nothing.
    """
    if mode != "live":
        return
    advance_family_cursors(conn, chat_id, delta, summary)


def _sync(
    conn: sqlite3.Connection,
    connector: MessageConnector,
    outcome: ScanOutcome,
    progress: Progress | None = None,
) -> None:
    """Pull all chats + messages from the connector into the store (live mode)."""
    connector.connect()
    chats_added = 0
    chats_updated = 0
    for chat in connector.list_chats():
        existing_id = store.chat_id_for_source(conn, chat.source_chat_id)
        if existing_id is None:
            chat_id = store.upsert_chat(conn, chat)
            chats_added += 1
        else:
            # Count a chat as *updated* only when its display name or type
            # actually differs — mirrors resync (src/db/sync.py) so the two sync
            # paths' sync_log bookkeeping agree instead of inflating updates with
            # every unchanged chat re-seen.
            chat_id = existing_id
            existing = store.get_chat(conn, existing_id)
            if existing is not None and (
                existing["display_name"] != chat.display_name
                or existing["chat_type"] != chat.chat_type
            ):
                store.upsert_chat(conn, chat)
                chats_updated += 1
        outcome.chats_synced += 1
        outcome.messages_synced += store.insert_messages(
            conn, chat_id, connector.fetch_messages(chat.source_chat_id)
        )
    connector.stop()
    # A sync_log row so a live scan's ingest is as visible as a resync's (#31).
    store.record_sync(
        conn,
        source="scan",
        chats_added=chats_added,
        chats_updated=chats_updated,
        messages_added=outcome.messages_synced,
    )
    _emit(
        progress,
        f"✓ synced {outcome.chats_synced} chats ({chats_added} new) / "
        f"{outcome.messages_synced} new messages",
    )


def _abort_offline(
    conn: sqlite3.Connection,
    config: Config,
    outcome: ScanOutcome,
    exc: ConnectorOffline,
    progress: Progress | None,
) -> ScanOutcome:
    """Finalize a live run that aborted because the source was offline.

    Records the run as failed with an empty funnel (no chats synced, no cursor
    advanced — the read-only guarantee holds since nothing was read or analysed),
    and best-effort alerts the notification channel so a scheduled run that would
    otherwise look green still reaches the operator.
    """
    outcome.notification_status = "offline"
    outcome.errors.append((0, str(exc)))
    _emit(progress, f"✗ aborted — WhatsApp source offline: {exc}")
    alert_status, _ = send_alert(
        config,
        f"⚠️ WhatsApp Radar: live scan aborted — source offline ({exc}). "
        "No messages were checked. Reconnect the WhatsApp sidecar.",
    )
    _emit(progress, f"• offline alert: {alert_status}")
    store.finish_run(conn, outcome.run_id, "failed", 0)
    store.record_run_funnel(
        conn,
        outcome.run_id,
        chats_synced=0,
        messages_synced=0,
        chats_monitored=0,
        stage1_passed=0,
        stage2_llm_calls=0,
        actionable=0,
        notification_status="offline",
    )
    return outcome


def scan(
    conn: sqlite3.Connection,
    config: Config,
    *,
    mode: Mode = "live",
    days: int | None = None,
    connector: MessageConnector | None = None,
    classifier: TracedClassifier | None = None,
    progress: Progress | None = None,
) -> ScanOutcome:
    """Run one scan: sync (live) -> analyze monitored deltas -> digest -> deliver.

    ``connector`` and ``classifier`` may be injected (tests, alternate wiring);
    otherwise they are built from ``config``. ``days`` windows the dry-run replay.
    ``progress`` receives human-readable stage lines for live output.
    """
    _emit(progress, f"▶ scan [{mode}] starting" + (f" (last {days} days)" if days else ""))
    run_id = store.start_run(conn, mode=mode, params_json=json.dumps({"days": days}))
    outcome = ScanOutcome(run_id=run_id, mode=mode)
    stage2 = classifier if classifier is not None else build_stage2_classifier(
        config.classifier, config.hub
    )

    if mode == "live":
        live_connector = connector if connector is not None else build_connector(config)
        try:
            # Liveness gate (#29): never sync from a dead/stale source. Relaunches
            # the sidecar once if it merely stopped; otherwise aborts loudly.
            preflight(config, live_connector, progress=progress)
        except ConnectorOffline as exc:
            return _abort_offline(conn, config, outcome, exc, progress)
        _sync(conn, live_connector, outcome, progress)
        # Transcribe downloaded voice notes BEFORE analysis so a voice note is never
        # classified as its "[voice note]" placeholder and the cursor never skips
        # real content (#36). No-op unless transcription is enabled; failures are
        # isolated per note and never block the analysis that follows. Dry-run skips
        # this — it replays stored messages with no network and no side effects.
        tr = run_transcription_phase(conn, config, progress=progress)
        outcome.transcriptions = tr.done
    else:
        _emit(progress, "• dry-run: replaying stored messages (no sync, no delivery)")

    monitored = store.monitored_chats(conn)
    outcome.chats_monitored = len(monitored)
    _emit(progress, f"• monitoring {outcome.chats_monitored} chat(s)")

    for chat in monitored:
        chat_id = int(chat["id"])
        delta = (
            store.family_delta_since_cursor(conn, chat_id)
            if mode == "live"
            else store.family_delta_replay(conn, chat_id, since_days=days)
        )
        # Hold the live delta before any voice note still awaiting transcription so
        # it is never analysed as a placeholder and the cursor never skips it (#36).
        # Gated on the feature flag: disabled → voice notes are ordinary messages.
        if mode == "live" and config.transcription.enabled:
            delta = hold_back_untranscribed(delta)
        if not delta:
            continue
        outcome.chats_with_delta += 1

        signal = has_actionable_signal(delta)
        if not signal.matched:
            # Stage 1 gate: record a not-actionable verdict without an LLM call.
            store.insert_analysis_item(
                conn,
                run_id,
                chat_id,
                action_required=False,
                priority=None,
                summary=None,
                suggested_next_action=None,
                deadline=None,
                confidence=None,
                evidence_message_ids_json=json.dumps([]),
            )
            _write_trace(
                conn, run_id, chat_id,
                signal=signal, delta=delta, outcome=None, result=_NOT_ACTIONABLE,
                final_action="not_actionable", telegram_text=None, error=None,
            )
            _advance(conn, mode, chat_id, delta, None)
            continue

        outcome.stage1_passed += 1
        _emit(
            progress,
            f"  • {chat['display_name']}: {len(delta)} new msg(s) passed Stage 1 "
            f"(keywords: {', '.join(signal.roots) or 'n/a'}) → Stage 2",
        )
        prior = recent_alert_context(
            conn, chat_id, since_days=config.hub.recent_alert_days, exclude_run_id=run_id
        )
        co = stage2.classify_traced(chat["display_name"], delta, prior)
        if co.llm_called:
            outcome.stage2_llm_calls += 1

        try:
            result = parse_analysis(co.raw_output)
        except ContractError as exc:
            # Do NOT persist an analysis item or advance the cursor: the same
            # delta is retried next run. The trace still captures the failure.
            # A budget overrun (stop_reason == 'max_tokens') is recorded as a
            # distinct, self-explanatory state rather than a generic contract
            # error, so a model swap that silently zeroes out classification is
            # obvious in the trace (issue #17).
            if co.stop_reason == "max_tokens":
                final_action = "llm_truncated"
                error = (
                    "model hit max_tokens before emitting parseable JSON "
                    f"(raw {len(co.raw_response or '')} chars) — use a model that "
                    "answers with JSON directly or raise hub.max_tokens"
                )
            else:
                final_action = "contract_error"
                error = str(exc)
            outcome.errors.append((chat_id, error))
            _write_trace(
                conn, run_id, chat_id,
                signal=signal, delta=delta, outcome=co, result=None,
                final_action=final_action, telegram_text=None, error=error,
            )
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

        telegram_text: str | None = None
        final_action = "not_actionable"
        if result.action_required:
            outcome.actionable += 1
            final_action = "actionable"
            telegram_text = render_item(
                DigestItem(
                    chat=chat["display_name"],
                    priority=result.priority,
                    summary=result.summary,
                    suggested_next_action=result.suggested_next_action,
                    deadline=result.deadline,
                    deadline_date=result.deadline_date,
                    confidence=result.confidence,
                    evidence_message_ids=result.evidence_message_ids,
                )
            )

        _write_trace(
            conn, run_id, chat_id,
            signal=signal, delta=delta, outcome=co, result=result,
            final_action=final_action, telegram_text=telegram_text, error=None,
        )
        _advance(conn, mode, chat_id, delta, result.summary)

    digest = build_digest(conn, run_id)
    outcome.digest = digest

    if mode == "dry_run":
        outcome.notification_status = "dry_run"
    elif digest.has_actionable_items:
        status, _ = deliver_digest(conn, config, run_id, digest)
        outcome.notification_status = status
    else:
        outcome.notification_status = "none"

    run_status = "completed_with_errors" if outcome.errors else "completed"
    store.finish_run(conn, run_id, run_status, outcome.chats_with_delta)
    store.record_run_funnel(
        conn,
        run_id,
        chats_synced=outcome.chats_synced,
        messages_synced=outcome.messages_synced,
        chats_monitored=outcome.chats_monitored,
        stage1_passed=outcome.stage1_passed,
        stage2_llm_calls=outcome.stage2_llm_calls,
        actionable=outcome.actionable,
        notification_status=outcome.notification_status,
        transcriptions=outcome.transcriptions,
    )
    _emit(
        progress,
        f"✓ done — transcribed {outcome.transcriptions}, Stage 1 {outcome.stage1_passed}, "
        f"LLM {outcome.stage2_llm_calls}, actionable {outcome.actionable}, "
        f"notify {outcome.notification_status}",
    )
    return outcome


def scan_outcome_to_dict(outcome: ScanOutcome) -> dict[str, Any]:
    """Serialize a :class:`ScanOutcome` to the structured result payload.

    This is the ``kind: "scan"`` result the Execution tab renders: the full
    funnel, the would-be / sent Telegram text, and the per-chat error list. Used
    by the CLI to emit the run's result sentinel.
    """
    digest = outcome.digest
    return {
        "kind": "scan",
        "ok": outcome.notification_status not in ("failed", "offline"),
        "run_id": outcome.run_id,
        "mode": outcome.mode,
        "funnel": {
            "chats_synced": outcome.chats_synced,
            "messages_synced": outcome.messages_synced,
            "chats_monitored": outcome.chats_monitored,
            "chats_with_delta": outcome.chats_with_delta,
            "transcriptions": outcome.transcriptions,
            "stage1_passed": outcome.stage1_passed,
            "stage2_llm_calls": outcome.stage2_llm_calls,
            "actionable": outcome.actionable,
        },
        "notification_status": outcome.notification_status,
        "telegram_text": digest.to_telegram_text() if digest and digest.has_actionable_items
        else None,
        "actionable_count": len(digest.items) if digest else 0,
        "errors": [{"chat_id": cid, "error": err} for cid, err in outcome.errors],
    }
