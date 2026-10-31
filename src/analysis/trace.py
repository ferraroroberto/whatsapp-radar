"""Per-chat audit trace written by the scan pipeline.

One ``analysis_trace`` row records the complete decision for one chat in one run:
the analysed delta, the Stage-1 keyword evidence, the exact LLM exchange and the
validated result.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Any

from src.analysis.classifier import ClassificationOutcome
from src.analysis.contract import AnalysisResult
from src.analysis.keywords import KeywordSignal, matched_rules
from src.db import store
from src.models import StoredMessage


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


def write_trace(
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
