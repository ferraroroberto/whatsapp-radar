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
from typing import Literal

from src.analysis.classifier import (
    ClassificationOutcome,
    TracedClassifier,
    build_stage2_classifier,
)
from src.analysis.contract import AnalysisResult, ContractError, parse_analysis
from src.analysis.keywords import KeywordSignal, has_actionable_signal
from src.analysis.review import prior_context
from src.config import Config
from src.connector.base import MessageConnector
from src.connector.factory import build_connector
from src.db import store
from src.models import StoredMessage
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
    """Advance the per-chat cursor (live mode only) after analysis is persisted."""
    if mode != "live":
        return
    last = delta[-1]
    rolling = json.dumps({"last_summary": summary, "last_message_id": last.source_message_id})
    store.advance_cursor(conn, chat_id, last.id, last.message_timestamp, rolling)


def _sync(conn: sqlite3.Connection, connector: MessageConnector, outcome: ScanOutcome) -> None:
    """Pull all chats + messages from the connector into the store (live mode)."""
    connector.connect()
    for chat in connector.list_chats():
        chat_id = store.upsert_chat(conn, chat)
        outcome.chats_synced += 1
        outcome.messages_synced += store.insert_messages(
            conn, chat_id, connector.fetch_messages(chat.source_chat_id)
        )
    connector.stop()


def scan(
    conn: sqlite3.Connection,
    config: Config,
    *,
    mode: Mode = "live",
    days: int | None = None,
    connector: MessageConnector | None = None,
    classifier: TracedClassifier | None = None,
) -> ScanOutcome:
    """Run one scan: sync (live) -> analyze monitored deltas -> digest -> deliver.

    ``connector`` and ``classifier`` may be injected (tests, alternate wiring);
    otherwise they are built from ``config``. ``days`` windows the dry-run replay.
    """
    run_id = store.start_run(conn, mode=mode, params_json=json.dumps({"days": days}))
    outcome = ScanOutcome(run_id=run_id, mode=mode)
    stage2 = classifier if classifier is not None else build_stage2_classifier(
        config.classifier, config.hub
    )

    if mode == "live":
        _sync(conn, connector if connector is not None else build_connector(config), outcome)

    monitored = store.monitored_chats(conn)
    outcome.chats_monitored = len(monitored)

    for chat in monitored:
        chat_id = int(chat["id"])
        delta = (
            store.messages_since_cursor(conn, chat_id)
            if mode == "live"
            else store.messages_for_chat(conn, chat_id, since_days=days)
        )
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
        prior = prior_context(store.get_rolling_context(conn, chat_id))
        co = stage2.classify_traced(chat["display_name"], delta, prior)
        if co.llm_called:
            outcome.stage2_llm_calls += 1

        try:
            result = parse_analysis(co.raw_output)
        except ContractError as exc:
            # Do NOT persist an analysis item or advance the cursor: the same
            # delta is retried next run. The trace still captures the failure.
            outcome.errors.append((chat_id, str(exc)))
            _write_trace(
                conn, run_id, chat_id,
                signal=signal, delta=delta, outcome=co, result=None,
                final_action="contract_error", telegram_text=None, error=str(exc),
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
    )
    return outcome
