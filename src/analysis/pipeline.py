"""Unified scan pipeline — the single callable App Launcher's Jobs tab fires.

``scan`` collapses the ingest -> Stage 1 (keyword prefilter) -> Stage 2 (LLM) ->
digest -> deliver flow into one process, and writes a full per-run audit trace so
every decision is inspectable. A missed important message is a real failure, so
for any run we can answer: what was synced, what passed the keyword stage, what
prompt went to the LLM, what it returned, and what was delivered.

Three modes:

- **live:** sync *all* chats from the connector (monitored and not), then analyze
  only monitored deltas since each cursor, deliver one digest, and advance each
  cursor ONLY after its analysis + trace are persisted — preserving the
  read-only, retry-safe guarantee of the review engine.
- **dry_run:** no connector; replay messages already in SQLite (optionally
  windowed to the last ``days``), ignoring the cursor. Advance no cursor and
  deliver nothing, but still build the would-be digest and record the full trace.
- **process:** the ``wr review`` verb — no connector, no sync: analyze
  monitored deltas since each cursor exactly as ``live`` does (same Stage 1/2
  gate, same trace, same reminder creation, same cursor advance), skipping only
  the sync/transcription/tripwire phases a live scan owns. ``deliver=False``
  (the verb's own ``--dry-run``) still advances cursors — it only withholds the
  digest, unlike ``dry_run`` mode above which touches no cursor at all.

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

from calendar_write import CalendarWriteClient

from src.analysis.classifier import (
    ClassificationOutcome,
    ClassifierUnavailable,
    TracedClassifier,
    build_stage2_classifier,
)
from src.analysis.contract import AnalysisResult, ContractError, parse_analysis
from src.analysis.keywords import KeywordSignal, has_actionable_signal, matched_rules
from src.analysis.reminders import (
    build_client as build_calendar_client,
)
from src.analysis.reminders import (
    create_reminder_event,
    is_eligible,
)
from src.analysis.review import (
    CLASSIFIER_OFFLINE_STATUS,
    abort_classifier_offline,
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
from src.progress import Progress, emit
from src.report.digest import Digest, DigestItem, build_digest, render_item

Mode = Literal["live", "dry_run", "process"]

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
    # Total messages across every chat whose delta was *successfully* recorded
    # this run — Stage 1 reject or Stage 2 success, never a contract error or an
    # aborted chat. A finer grain than ``chats_with_delta``, available for
    # every mode.
    messages_processed: int = 0
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
    read from the parsed result's ``evidence_message_ids``.
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
    """Advance the family's per-member cursors after persistence (live + process).

    The merged delta spans the head and any linked children; each keeps its own
    ingestion-id cursor, so advancement is grouped by origin chat rather
    than a single high-water mark. Dry-run advances nothing — it replays the
    same stored messages on every call, which would otherwise mean the next
    replay never sees them again.
    """
    if mode == "dry_run":
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
        emit(progress, f"⚠ {source} sync failed — {error}; its cursors are held")
    emit(
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
    emit(progress, f"✗ aborted — all enabled sources offline: {exc}")
    alert_status, _ = send_alert(
        config,
        f"⚠️ Family Radar: live scan aborted — all sources offline ({exc}). "
        "No messages were checked. Restore at least one enabled source.",
    )
    emit(progress, f"• offline alert: {alert_status}")
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


def _abort_classifier(
    conn: sqlite3.Connection,
    config: Config,
    outcome: ScanOutcome,
    exc: ClassifierUnavailable,
    progress: Progress | None,
    *,
    chat_id: int = 0,
) -> ScanOutcome:
    """Finalize a live/dry/process run that aborted because Stage 2 could not reach the hub.

    The mirror image of :func:`_abort_offline` at the other end of the pipeline:
    a source outage aborts loudly, alerts, and records the run as ``failed``,
    and a *classifier* outage does the same instead of raising straight out of
    ``scan`` and stranding the run row at ``status='running'``. The funnel keeps the
    counters earned before the failure — the chats analysed up to that point are
    real work — and every remaining cursor is held. ``chat_id`` names the chat
    whose classify call actually hit the dead hub — 0 only when no chat was
    in flight yet.
    """
    outcome.notification_status = CLASSIFIER_OFFLINE_STATUS
    outcome.errors.append((chat_id, str(exc)))
    abort_classifier_offline(
        conn,
        config,
        outcome.run_id,
        exc,
        chats_with_delta=outcome.chats_with_delta,
        progress=progress,
    )
    store.record_run_funnel(
        conn,
        outcome.run_id,
        chats_synced=outcome.chats_synced,
        messages_synced=outcome.messages_synced,
        chats_monitored=outcome.chats_monitored,
        stage1_passed=outcome.stage1_passed,
        stage2_llm_calls=outcome.stage2_llm_calls,
        actionable=outcome.actionable,
        notification_status=CLASSIFIER_OFFLINE_STATUS,
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
    emit(
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
            emit(progress, "⚠ tripwire nudge state was invalid; treating it as unsent")
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
        f"Family Radar: {len(result.hits)} unmonitored chat(s) matched actionable "
        f"keywords recently: {', '.join(names)}{suffix}. Open Messages → "
        "Chats worth monitoring to review and promote them.",
    )
    outcome.tripwire_nudge_status = status
    if status == "sent":
        store.mark_tripwire_nudge_sent(conn, now.isoformat(timespec="seconds"))
        emit(progress, "• tripwire weekly nudge: sent")
    else:
        emit(progress, f"⚠ tripwire weekly nudge: {status}" + (f" — {detail}" if detail else ""))
    return result


def _begin_scan(
    conn: sqlite3.Connection,
    config: Config,
    *,
    mode: Mode,
    days: int | None,
    classifier: TracedClassifier | None,
    calendar_client: CalendarWriteClient | None,
    progress: Progress | None,
) -> tuple[ScanOutcome, TracedClassifier, CalendarWriteClient | None]:
    """Start the run row and resolve the Stage-2 classifier and reminder client."""
    emit(
        progress,
        "▶ process starting — analyzing monitored chats since last cursor"
        if mode == "process"
        else f"▶ scan [{mode}] starting" + (f" (last {days} days)" if days else ""),
    )
    run_id = store.start_run(
        conn, mode=mode, params_json=json.dumps({"days": days}),
        kind="process" if mode == "process" else "scan",
    )
    outcome = ScanOutcome(run_id=run_id, mode=mode)
    for source in config.sources:
        ensure_source_funnel(outcome.source_funnels, source)
    stage2 = classifier if classifier is not None else build_stage2_classifier(
        config.classifier, config.hub, config.children
    )
    if (
        calendar_client is None
        and mode in ("live", "process")
        and config.family.reminder_calendar_id
    ):
        try:
            calendar_client = build_calendar_client(config.calendar)
        except (FileNotFoundError, RuntimeError) as exc:
            emit(progress, f"⚠ calendar reminders disabled: {exc}")
    return outcome, stage2, calendar_client


def _live_bindings(
    config: Config,
    connector: MessageConnector | None,
    connectors: list[ConnectorBinding] | None,
) -> list[ConnectorBinding]:
    if connectors is not None:
        return connectors
    if connector is not None:
        return [ConnectorBinding(source="whatsapp", connector=connector)]
    return build_connectors(config)


def _sync_or_offline(
    conn: sqlite3.Connection,
    config: Config,
    bindings: list[ConnectorBinding],
    outcome: ScanOutcome,
    progress: Progress | None,
) -> set[str]:
    """Sync every source; raise :class:`ConnectorOffline` when none came up."""
    successful_sources = _sync(conn, config, bindings, outcome, progress)
    if not successful_sources:
        detail = "; ".join(f"{source}: {error}" for source, error in outcome.source_errors)
        raise ConnectorOffline(
            ConnectorStatus(name="all_sources", connected=False, detail=detail)
        )
    return successful_sources


def _chat_delta(
    conn: sqlite3.Connection,
    config: Config,
    chat_id: int,
    *,
    mode: Mode,
    days: int | None,
    successful_sources: set[str],
) -> list[StoredMessage]:
    """The messages of one monitored chat this run should analyse."""
    delta = (
        store.family_delta_replay(conn, chat_id, since_days=days)
        if mode == "dry_run"
        else store.family_delta_since_cursor(conn, chat_id)
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
    # it is never analysed as a placeholder and the cursor never skips it.
    # Gated on the feature flag: disabled → voice notes are ordinary messages.
    return hold_back_if_transcribing(
        delta, mode != "dry_run" and config.transcription.enabled
    )


def _reject_at_stage1(
    conn: sqlite3.Connection,
    outcome: ScanOutcome,
    source_funnel: SourceFunnel,
    chat_id: int,
    *,
    mode: Mode,
    source: str,
    signal: KeywordSignal,
    delta: list[StoredMessage],
) -> None:
    """Stage 1 gate: record a not-actionable verdict without an LLM call."""
    source_funnel.stage1_rejected += 1
    store.insert_analysis_item(
        conn,
        outcome.run_id,
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
        conn, outcome.run_id, chat_id,
        source=source, signal=signal, delta=delta, outcome=None,
        result=_NOT_ACTIONABLE,
        final_action="not_actionable", telegram_text=None, error=None,
    )
    outcome.messages_processed += len(delta)
    _advance(conn, mode, chat_id, delta, None)
    if mode in ("live", "process"):
        source_funnel.cursors_advanced += 1


def _contract_failure(co: ClassificationOutcome, exc: ContractError) -> tuple[str, str]:
    """The trace ``final_action`` and error text for an unparseable Stage-2 answer.

    A budget overrun (stop_reason == 'max_tokens') is recorded as a distinct,
    self-explanatory state rather than a generic contract error, so a model swap
    that silently zeroes out classification is obvious in the trace.
    """
    if co.stop_reason == "max_tokens":
        return "llm_truncated", (
            "model hit max_tokens before emitting parseable JSON "
            f"(raw {len(co.raw_response or '')} chars) — use a model that "
            "answers with JSON directly or raise hub.max_tokens"
        )
    return "contract_error", str(exc)


def _create_reminder_if_eligible(
    conn: sqlite3.Connection,
    config: Config,
    calendar_client: CalendarWriteClient | None,
    chat: sqlite3.Row,
    *,
    mode: Mode,
    item_id: int,
    result: AnalysisResult,
) -> None:
    # Additive only — never touches the Telegram digest path.
    # live + process only: dry-run replays the same messages every call, which
    # would otherwise mint a fresh duplicate event each time. The
    # reminder-calendar check guards even an injected client (tests) against
    # writing to an unconfigured (empty-string) calendar id.
    if (
        mode in ("live", "process")
        and calendar_client is not None
        and config.family.reminder_calendar_id
        and is_eligible(result)
    ):
        create_reminder_event(
            conn,
            calendar_client,
            calendar_id=config.family.reminder_calendar_id,
            family=config.family,
            chat_id=int(chat["id"]),
            item_id=item_id,
            chat_display_name=chat["display_name"],
            result=result,
        )


def _actionable_text(chat: sqlite3.Row, result: AnalysisResult) -> str:
    return render_item(
        DigestItem(
            chat=chat["display_name"],
            priority=result.priority,
            summary=result.summary,
            suggested_next_action=result.suggested_next_action,
            deadline=result.deadline,
            deadline_date=result.deadline_date,
            confidence=result.confidence,
            evidence_message_ids=result.evidence_message_ids,
            prep_complexity=result.prep_complexity,
        )
    )


def _analyze_chat(
    conn: sqlite3.Connection,
    config: Config,
    stage2: TracedClassifier,
    calendar_client: CalendarWriteClient | None,
    outcome: ScanOutcome,
    chat: sqlite3.Row,
    delta: list[StoredMessage],
    *,
    mode: Mode,
    progress: Progress | None,
) -> ScanOutcome | None:
    """Run Stage 1 and Stage 2 on one chat's delta and persist the verdict.

    Returns the finalized outcome when the run must stop (Stage 2 unreachable),
    else ``None`` so the scan moves on to the next chat.
    """
    run_id = outcome.run_id
    chat_id = int(chat["id"])
    source = str(chat["source"])
    source_funnel = note_delta_funnel(outcome.source_funnels, source, len(delta))
    signal = has_actionable_signal(delta, source)
    if not signal.matched:
        _reject_at_stage1(
            conn, outcome, source_funnel, chat_id,
            mode=mode, source=source, signal=signal, delta=delta,
        )
        return None

    outcome.stage1_passed += 1
    source_funnel.stage1_passed += 1
    emit(
        progress,
        f"  • {chat['display_name']}: {len(delta)} new msg(s) passed Stage 1 "
        f"(keywords: {', '.join(signal.roots) or 'n/a'}) → Stage 2",
    )
    prior = recent_alert_context(
        conn, chat_id, since_days=config.hub.recent_alert_days, exclude_run_id=run_id
    )
    try:
        co = stage2.classify_traced(
            chat["display_name"], delta, prior, source=source
        )
    except ClassifierUnavailable as exc:
        # Whole-run condition, not a per-chat one: every remaining chat would
        # hit the same dead hub, so stop once instead of looping through a
        # timeout per chat.
        return _abort_classifier(conn, config, outcome, exc, progress, chat_id=chat_id)
    if co.llm_called:
        outcome.stage2_llm_calls += 1
        source_funnel.llm_calls += 1

    try:
        result = parse_analysis(co.raw_output)
    except ContractError as exc:
        # Do NOT persist an analysis item or advance the cursor: the same
        # delta is retried next run. The trace still captures the failure.
        final_action, error = _contract_failure(co, exc)
        outcome.errors.append((chat_id, error))
        _write_trace(
            conn, run_id, chat_id,
            source=source, signal=signal, delta=delta, outcome=co, result=None,
            final_action=final_action, telegram_text=None, error=error,
        )
        return None

    item_id = persist_analysis_result(conn, run_id, chat_id, result)
    outcome.messages_processed += len(delta)
    _create_reminder_if_eligible(
        conn, config, calendar_client, chat, mode=mode, item_id=item_id, result=result
    )

    telegram_text: str | None = None
    final_action = "not_actionable"
    if result.action_required:
        outcome.actionable += 1
        source_funnel.actionable += 1
        final_action = "actionable"
        telegram_text = _actionable_text(chat, result)

    _write_trace(
        conn, run_id, chat_id,
        source=source, signal=signal, delta=delta, outcome=co, result=result,
        final_action=final_action, telegram_text=telegram_text, error=None,
    )
    _advance(conn, mode, chat_id, delta, result.summary)
    if mode in ("live", "process"):
        source_funnel.cursors_advanced += 1
    return None


def _finish_scan(
    conn: sqlite3.Connection,
    config: Config,
    outcome: ScanOutcome,
    *,
    mode: Mode,
    deliver: bool,
    progress: Progress | None,
) -> ScanOutcome:
    """Build the digest, deliver it when due, and close the run row."""
    run_id = outcome.run_id
    digest = build_digest(conn, run_id)
    outcome.digest = digest

    if mode == "dry_run":
        outcome.notification_status = "dry_run"
    elif not deliver:
        # The ``process`` verb's own ``--dry-run``: cursors already advanced
        # above, but the digest is withheld rather than sent.
        outcome.notification_status = "dry_run" if digest.has_actionable_items else "none"
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
    emit(
        progress,
        f"✓ done — transcribed {outcome.transcriptions}, Stage 1 {outcome.stage1_passed}, "
        f"LLM {outcome.stage2_llm_calls}, actionable {outcome.actionable}, "
        f"notify {outcome.notification_status}",
    )
    return outcome


def scan(
    conn: sqlite3.Connection,
    config: Config,
    *,
    mode: Mode = "live",
    days: int | None = None,
    connector: MessageConnector | None = None,
    connectors: list[ConnectorBinding] | None = None,
    classifier: TracedClassifier | None = None,
    calendar_client: CalendarWriteClient | None = None,
    deliver: bool = True,
    progress: Progress | None = None,
) -> ScanOutcome:
    """Run one scan: sync (live) -> analyze monitored deltas -> digest -> deliver.

    ``connector`` and ``classifier`` may be injected (tests, alternate wiring);
    otherwise they are built from ``config``. ``days`` windows the dry-run replay.
    ``progress`` receives human-readable stage lines for live output.

    ``calendar_client`` may also be injected (tests); otherwise it is built lazily
    from ``config.calendar.write_token_path`` the first time it's needed, and only
    when ``config.family.reminder_calendar_id`` is configured — an unset reminder
    calendar or a missing/invalid write token both leave it ``None``, which simply
    means no routine-prep reminder events are created this run. Reminder
    creation happens for ``live`` and ``process`` alike: dry-run replays the same
    messages on every call, which would otherwise mint a fresh duplicate event
    each time.

    ``deliver=False`` is the ``process`` verb's own ``--dry-run``: the
    digest is still built and traced, but withheld rather than sent. It has no
    effect on ``dry_run`` mode, which already never delivers.
    """
    outcome, stage2, calendar_client = _begin_scan(
        conn, config, mode=mode, days=days, classifier=classifier,
        calendar_client=calendar_client, progress=progress,
    )
    successful_sources: set[str] = set()
    if mode == "live":
        live_bindings = _live_bindings(config, connector, connectors)
        try:
            successful_sources = _sync_or_offline(conn, config, live_bindings, outcome, progress)
        except ConnectorOffline as exc:
            return _abort_offline(conn, config, outcome, exc, progress)
        # Transcribe downloaded voice notes BEFORE analysis so a voice note is never
        # classified as its "[voice note]" placeholder and the cursor never skips
        # real content. No-op unless transcription is enabled; failures are
        # isolated per note and never block the analysis that follows. Dry-run skips
        # this — it replays stored messages with no network and no side effects.
        tr = run_transcription_phase(conn, config, progress=progress)
        outcome.transcriptions = tr.done
        _run_tripwire(conn, config, outcome, progress)
    elif mode == "dry_run":
        emit(progress, "• dry-run: replaying stored messages (no sync, no delivery)")
    else:
        emit(progress, "• process: analyzing monitored deltas since last cursor (no sync)")

    monitored = store.monitored_chats(conn)
    outcome.chats_monitored = len(monitored)
    for chat in monitored:
        ensure_source_funnel(
            outcome.source_funnels, str(chat["source"])
        ).monitored_channels += 1
    emit(progress, f"• monitoring {outcome.chats_monitored} chat(s)")

    for chat in monitored:
        delta = _chat_delta(
            conn, config, int(chat["id"]),
            mode=mode, days=days, successful_sources=successful_sources,
        )
        if not delta:
            continue
        outcome.chats_with_delta += 1
        aborted = _analyze_chat(
            conn, config, stage2, calendar_client, outcome, chat, delta,
            mode=mode, progress=progress,
        )
        if aborted is not None:
            return aborted

    return _finish_scan(conn, config, outcome, mode=mode, deliver=deliver, progress=progress)


def scan_outcome_to_dict(outcome: ScanOutcome) -> dict[str, Any]:
    """Serialize a :class:`ScanOutcome` to the structured result payload.

    This is the ``kind: "scan"``/``"process"`` result the Execution tab renders
    (the DB row's own ``kind`` column, set the same way in :func:`scan`): the
    full funnel, the would-be / sent Telegram text, and the per-chat error list.
    Used by the CLI to emit the run's result sentinel.
    """
    digest = outcome.digest
    return {
        "kind": "process" if outcome.mode == "process" else "scan",
        "ok": (
            outcome.notification_status
            not in ("failed", "offline", CLASSIFIER_OFFLINE_STATUS)
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
