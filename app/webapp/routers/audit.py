"""Audit tab (#12): per-run trace drill-down — the surface that makes the radar
trustworthy.

A missed important message is a real failure, so for any run the operator must be
able to answer *why* each message was or wasn't promoted. After #7 every run
persists to ``review_runs`` (mode, params, funnel counters) and one
``analysis_trace`` row per (run, chat) capturing the full decision record — input
delta, Stage-1 keyword roots, the exact LLM system+user prompts, the raw model
response, the parsed verdict, the final action, and the Telegram text.

This router is **read-only** (SELECT only, no writes, no cursor changes) so it is
safe on the request path. It exposes two endpoints: a run list (with the funnel)
and a per-run drill-down (the per-chat trace). It never triggers scans — that is
the Execution tab (#11).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from src.config import load_config
from src.db import store

router = APIRouter()

# sync_log sources that represent data-maintenance runs (not review/scan runs).
# Surfaced in the audit timeline so resync/reprocess are visible alongside runs;
# 'scan'-sourced syncs are omitted because scans already appear as review_runs.
_MAINTENANCE_SOURCES = {"resync", "reprocess"}


def _db_path(request: Request) -> Path:
    # Tests/e2e inject a fixture DB via app.state.db_path; fall back to config.
    path = getattr(request.app.state, "db_path", None)
    return Path(path) if path is not None else load_config().db_path


def _loads(value: Any) -> Any:
    """Parse a stored JSON column, tolerating null/blank/legacy non-JSON text."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _run_list_row(row: sqlite3.Row) -> dict[str, Any]:
    """Shape a review_runs row for the run list: identity + parsed params + funnel."""
    return {
        "id": int(row["id"]),
        "mode": row["mode"],
        "status": row["status"],
        "params": _loads(row["params_json"]),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "notification_status": row["notification_status"],
        "error": row["error"],
        "funnel": {
            "chats_synced": int(row["chats_synced"]),
            "messages_synced": int(row["messages_synced"]),
            "chats_monitored": int(row["chats_monitored"]),
            "chats_reviewed": int(row["chats_reviewed"]),
            "stage1_passed": int(row["stage1_passed"]),
            "stage2_llm_calls": int(row["stage2_llm_calls"]),
            "actionable": int(row["actionable"]),
            "voice_transcribed": int(row["voice_transcribed"]),
            "voice_failed": int(row["voice_failed"]),
            "voice_skipped_old": int(row["voice_skipped_old"]),
        },
    }


def _trace_row(row: sqlite3.Row) -> dict[str, Any]:
    """Shape one analysis_trace row (joined to chat name) into the full decision record."""
    return {
        "chat_id": int(row["chat_id"]),
        "display_name": row["display_name"],
        "input_message_ids": _loads(row["input_message_ids_json"]),
        "input_text": row["input_text"],
        "messages": _loads(row["messages_json"]),
        "stage1_passed": bool(row["stage1_passed"]),
        "stage1_roots": _loads(row["stage1_roots_json"]),
        "llm_called": bool(row["llm_called"]),
        "llm_system_prompt": row["llm_system_prompt"],
        "llm_user_prompt": row["llm_user_prompt"],
        "llm_raw_response": row["llm_raw_response"],
        "parsed_result": _loads(row["parsed_result_json"]),
        "final_action": row["final_action"],
        "telegram_text": row["telegram_text"],
        "error": row["error"],
    }


@router.get("/api/audit/runs")
async def list_runs(request: Request, limit: int = 50) -> dict[str, Any]:
    """Recent review/scan runs (with funnel) plus maintenance sync markers.

    ``runs`` are the inspectable review/scan runs, newest first; ``syncs`` are the
    resync/reprocess data-maintenance events so they're visible in the same audit
    timeline (read-only, no schema beyond the existing sync_log).
    """
    limit = max(1, min(limit, 200))
    conn = store.connect(_db_path(request))
    try:
        runs = [_run_list_row(r) for r in store.list_review_runs(conn, limit)]
        syncs = [
            {
                "ran_at": s["ran_at"],
                "source": s["source"],
                "chats_added": int(s["chats_added"]),
                "chats_updated": int(s["chats_updated"]),
                "messages_added": int(s["messages_added"]),
                "voice_notes_added": int(s["voice_notes_added"]),
            }
            for s in store.recent_syncs(conn, limit)
            if s["source"] in _MAINTENANCE_SOURCES
        ]
    finally:
        conn.close()
    return {"runs": runs, "syncs": syncs}


@router.get("/api/audit/runs/{run_id}")
async def get_run(request: Request, run_id: int) -> dict[str, Any]:
    """One run's header + funnel and its per-chat audit trace (the drill-down)."""
    conn = store.connect(_db_path(request))
    try:
        run = store.review_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        traces = [_trace_row(t) for t in store.traces_for_run(conn, run_id)]
        return {"run": _run_list_row(run), "traces": traces}
    finally:
        conn.close()
