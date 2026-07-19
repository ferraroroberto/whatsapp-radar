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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from src.analysis._common import Progress, _emit
from src.analysis.classifier import (
    ClassificationOutcome,
    TracedClassifier,
    build_stage2_classifier,
)
from src.analysis.contract import AnalysisResult, ContractError, parse_analysis
from src.analysis.keywords import KeywordSignal, has_actionable_signal, matched_rules
from src.analysis.review import (
    advance_family_cursors,
    hold_back_if_transcribing,
    note_delta_funnel,
    persist_analysis_result,
    recent_alert_context,
)
from src.analysis.source_funnel import (
    SourceFunnel,
    ensure_source_funnel,
    source_funnels_dict,
    source_funnels_json,
)
from src.analysis.transcription import run_transcription_phase
from src.analysis.tripwire import TripwireScan, scan_tripwire
from src.config import Config
from src.connector.base import ConnectorStatus, MessageConnector
from src.connector.factory import ConnectorBinding, build_connectors
from src.connector.preflight import ConnectorOffline, preflight
from src.db import store
from src.db.sync import sync_sources
from src.models import StoredMessage
from src.notify.alert import send_alert
from src.notify.delivery import deliver_digest
from src.report.digest import Digest, DigestItem, build_digest, render_item

Mode = Literal["live", "dry_run"]

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
    tripwire_scanned: int = 0
    tripwire_hits: int = 0
    tripwire_truncated: bool = False
    tripwire_nudge_status: str = "disabled"
    notification_status: str = "none"
    errors: list[tuple[int, str]] = field(default_factory=list)
    source_errors: list[tuple[str, str]] = field(default_factory=list)
    source_funnels: dict[str, SourceFunnel] = field(default_factory=dict)
    digest: Digest | None = None


def _render_delta(delta: list[StoredMessage]) -> str:
    return "\n".join(
        f"[{m.source_message_id}] {m.sender_label or 'unknown'}: {m.text or ''}" for m in delta
    )


def _messages_record(delta: list[StoredMessage], source: str) -> str:
    """Per-message audit record: each message with the Stage-1 roots it matched.

    Captured at run time (not recomputed on read) so the trace stays faithful to
    the keyword roots that actually ran, even if ``keyword_roots.txt`` changes
    later. The LLM's per-message verdict is *not* stored here — it is derived on
    read from the parsed result's ``evidence_message_ids`` (issue #12).
    """
    records: list[dict[str, Any]] = []
    for message in delta:
        rules = matched_rules(message.text, source)
        records.append(
            {
                "id": message.source_message_id,
                "sender": message.sender_label,
                "text": message.text,
                "type": message.message_type,
                "roots": [rule.root for rule in rules],
                "buckets": list(dict.fromkeys(rule.bucket for rule in rules)),
            }
        )
    return json.dumps(records, ensure_ascii=False)


def _result_json(result: AnalysisResult) -> str:
    return json.dumps(asdict(result), ensure_ascii=False)


def _write_trace(
    conn: sqlite3.Connection,
    run_id: int,
    chat_id: int,
    *,
    source: str,
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
        messages_json=_messages_record(delta, source),
        stage1_passed=signal.matched,
        stage1_roots_json=json.dumps(list(signal.roots)),
        stage1_buckets_json=json.dumps(list(signal.buckets)),
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
    config: Config,
    bindings: list[ConnectorBinding],
    outcome: ScanOutcome,
    progress: Progress | None = None,
) -> set[str]:
    """Pull all chats + messages from the connector into the store (live mode)."""
    synced = sync_sources(
        conn,
        bindings,
        operation="scan",
        prepare=lambda source, connector: preflight(
            config,
            connector,
            source=source,
            progress=progress,
        ),
        gmail_retention_days=config.gmail.retention_days,
        progress=progress,
    )
    delta = synced.delta
    outcome.source_errors.extend(synced.source_errors)
    outcome.chats_synced += delta.chats_seen
    outcome.messages_synced += delta.messages_added
    for result in synced.results:
        source_funnel = ensure_source_funnel(outcome.source_funnels, result.source)
        source_funnel.sync_status = "success" if result.ok else "failed"
        source_funnel.sync_error = result.error
        source_funnel.chats_synced = result.delta.chats_seen
        source_funnel.messages_synced = result.delta.messages_added
    for source, error in synced.source_errors:
        _emit(progress, f"⚠ {source} sync failed — {error}; its cursors are held")
    _emit(
        progress,
        f"✓ synced {outcome.chats_synced} chats ({delta.chats_added} new) / "
        f"{outcome.messages_synced} new messages",
    )
    return synced.successful_sources


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
    _emit(progress, f"✗ aborted — all enabled sources offline: {exc}")
    alert_status, _ = send_alert(
        config,
        f"⚠️ WhatsApp Radar: live scan aborted — all sources offline ({exc}). "
        "No messages were checked. Restore at least one enabled source.",
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
        source_funnel_json=source_funnels_json(outcome.source_funnels),
    )
    return outcome


def _run_tripwire(
    conn: sqlite3.Connection,
    config: Config,
    outcome: ScanOutcome,
    progress: Progress | None,
) -> TripwireScan:
    """Run the additive Stage-1-only discovery pass and optional weekly nudge."""
    result = scan_tripwire(conn, config.tripwire)
    outcome.tripwire_scanned = result.scanned_messages
    outcome.tripwire_hits = len(result.hits)
    outcome.tripwire_truncated = result.truncated
    _emit(
        progress,
        f"• tripwire: scanned {result.scanned_messages} recent unmonitored message(s), "
        f"found {len(result.hits)} chat(s)" + (" (bounded)" if result.truncated else ""),
    )
    if not config.tripwire.telegram_nudge_enabled:
        return result
    if not result.hits:
        outcome.tripwire_nudge_status = "no_hits"
        return result

    now = datetime.now(UTC)
    last_raw = store.last_tripwire_nudge_at(conn)
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
        except ValueError:
            last = None
            _emit(progress, "⚠ tripwire nudge state was invalid; treating it as unsent")
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if last is not None and now - last < timedelta(
            days=config.tripwire.nudge_cadence_days
        ):
            outcome.tripwire_nudge_status = "cadenced"
            return result

    names = [hit.display_name for hit in result.hits[:8]]
    suffix = f" (+{len(result.hits) - len(names)} more)" if len(result.hits) > len(names) else ""
    status, detail = send_alert(
        config,
        f"WhatsApp Radar: {len(result.hits)} unmonitored chat(s) matched actionable "
        f"keywords recently: {', '.join(names)}{suffix}. Open Messages → "
        "Chats worth monitoring to review and promote them.",
    )
    outcome.tripwire_nudge_status = status
    if status == "sent":
        store.mark_tripwire_nudge_sent(conn, now.isoformat(timespec="seconds"))
        _emit(progress, "• tripwire weekly nudge: sent")
    else:
        _emit(progress, f"⚠ tripwire weekly nudge: {status}" + (f" — {detail}" if detail else ""))
    return result


def scan(
    conn: sqlite3.Connection,
    config: Config,
    *,
    mode: Mode = "live",
    days: int | None = None,
    connector: MessageConnector | None = None,
    connectors: list[ConnectorBinding] | None = None,
    classifier: TracedClassifier | None = None,
    progress: Progress | None = None,
) -> ScanOutcome:
    """Run one scan: sync (live) -> analyze monitored deltas -> digest -> deliver.

    ``connector`` and ``classifier`` may be injected (tests, alternate wiring);
    otherwise they are built from ``config``. ``days`` windows the dry-run replay.
    ``progress`` receives human-readable stage lines for live output.
    """
    _emit(progress, f"▶ scan [{mode}] starting" + (f" (last {days} days)" if days else ""))
    run_id = store.start_run(
        conn, mode=mode, params_json=json.dumps({"days": days}), kind="scan"
    )
    outcome = ScanOutcome(run_id=run_id, mode=mode)
    for source in config.sources:
        ensure_source_funnel(outcome.source_funnels, source)
    stage2 = classifier if classifier is not None else build_stage2_classifier(
        config.classifier, config.hub
    )

    if mode == "live":
        live_bindings = connectors
        if live_bindings is None:
            live_bindings = (
                [ConnectorBinding(source="whatsapp", connector=connector)]
                if connector is not None
                else build_connectors(config)
            )
        try:
            successful_sources = _sync(conn, config, live_bindings, outcome, progress)
            if not successful_sources:
                detail = "; ".join(
                    f"{source}: {error}" for source, error in outcome.source_errors
                )
                raise ConnectorOffline(
                    ConnectorStatus(name="all_sources", connected=False, detail=detail)
                )
        except ConnectorOffline as exc:
            return _abort_offline(conn, config, outcome, exc, progress)
        # Transcribe downloaded voice notes BEFORE analysis so a voice note is never
        # classified as its "[voice note]" placeholder and the cursor never skips
        # real content (#36). No-op unless transcription is enabled; failures are
        # isolated per note and never block the analysis that follows. Dry-run skips
        # this — it replays stored messages with no network and no side effects.
        tr = run_transcription_phase(conn, config, progress=progress)
        outcome.transcriptions = tr.done
        _run_tripwire(conn, config, outcome, progress)
    else:
        _emit(progress, "• dry-run: replaying stored messages (no sync, no delivery)")

    monitored = store.monitored_chats(conn)
    outcome.chats_monitored = len(monitored)
    for chat in monitored:
        ensure_source_funnel(
            outcome.source_funnels, str(chat["source"])
        ).monitored_channels += 1
    _emit(progress, f"• monitoring {outcome.chats_monitored} chat(s)")

    for chat in monitored:
        chat_id = int(chat["id"])
        delta = (
            store.family_delta_since_cursor(conn, chat_id)
            if mode == "live"
            else store.family_delta_replay(conn, chat_id, since_days=days)
        )
        if mode == "live":
            # A failed source may have older cached messages waiting. Excluding
            # those messages from this run ensures _advance only moves cursors
            # for sources that were confirmed live and successfully synced.
            source_by_chat: dict[int, str | None] = {}
            filtered_delta: list[StoredMessage] = []
            for message in delta:
                if message.chat_id not in source_by_chat:
                    chat_row = store.get_chat(conn, message.chat_id)
                    source_by_chat[message.chat_id] = (
                        str(chat_row["source"]) if chat_row is not None else None
                    )
                if source_by_chat[message.chat_id] in successful_sources:
                    filtered_delta.append(message)
            delta = filtered_delta
        # Hold the live delta before any voice note still awaiting transcription so
        # it is never analysed as a placeholder and the cursor never skips it (#36).
        # Gated on the feature flag: disabled → voice notes are ordinary messages.
        delta = hold_back_if_transcribing(
            delta, mode == "live" and config.transcription.enabled
        )
        if not delta:
            continue
        outcome.chats_with_delta += 1

        source = str(chat["source"])
        source_funnel = note_delta_funnel(outcome.source_funnels, source, len(delta))
        signal = has_actionable_signal(delta, source)
        if not signal.matched:
            source_funnel.stage1_rejected += 1
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
                source=source, signal=signal, delta=delta, outcome=None,
                result=_NOT_ACTIONABLE,
                final_action="not_actionable", telegram_text=None, error=None,
            )
            _advance(conn, mode, chat_id, delta, None)
            if mode == "live":
                source_funnel.cursors_advanced += 1
            continue

        outcome.stage1_passed += 1
        source_funnel.stage1_passed += 1
        _emit(
            progress,
            f"  • {chat['display_name']}: {len(delta)} new msg(s) passed Stage 1 "
            f"(keywords: {', '.join(signal.roots) or 'n/a'}) → Stage 2",
        )
        prior = recent_alert_context(
            conn, chat_id, since_days=config.hub.recent_alert_days, exclude_run_id=run_id
        )
        co = stage2.classify_traced(
            chat["display_name"], delta, prior, source=source
        )
        if co.llm_called:
            outcome.stage2_llm_calls += 1
            source_funnel.llm_calls += 1

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
                source=source, signal=signal, delta=delta, outcome=co, result=None,
                final_action=final_action, telegram_text=None, error=error,
            )
            continue

        persist_analysis_result(conn, run_id, chat_id, result)

        telegram_text: str | None = None
        final_action = "not_actionable"
        if result.action_required:
            outcome.actionable += 1
            source_funnel.actionable += 1
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
            source=source, signal=signal, delta=delta, outcome=co, result=result,
            final_action=final_action, telegram_text=telegram_text, error=None,
        )
        _advance(conn, mode, chat_id, delta, result.summary)
        if mode == "live":
            source_funnel.cursors_advanced += 1

    digest = build_digest(conn, run_id)
    outcome.digest = digest

    if mode == "dry_run":
        outcome.notification_status = "dry_run"
    elif digest.has_actionable_items:
        status, _ = deliver_digest(conn, config, run_id, digest)
        outcome.notification_status = status
    else:
        outcome.notification_status = "none"

    run_status = (
        "completed_with_errors"
        if outcome.errors or outcome.source_errors
        else "completed"
    )
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
        source_funnel_json=source_funnels_json(outcome.source_funnels),
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
        "ok": (
            outcome.notification_status not in ("failed", "offline")
            and not outcome.source_errors
        ),
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
        "tripwire": {
            "scanned_messages": outcome.tripwire_scanned,
            "hits": outcome.tripwire_hits,
            "truncated": outcome.tripwire_truncated,
            "nudge_status": outcome.tripwire_nudge_status,
        },
        "telegram_text": digest.to_telegram_text() if digest and digest.has_actionable_items
        else None,
        "actionable_count": len(digest.items) if digest else 0,
        "errors": [{"chat_id": cid, "error": err} for cid, err in outcome.errors],
        "source_errors": [
            {"source": source, "error": error}
            for source, error in outcome.source_errors
        ],
        "sources": source_funnels_dict(outcome.source_funnels),
    }
