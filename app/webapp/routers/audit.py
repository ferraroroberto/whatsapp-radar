"""Audit tab (#12): per-run trace drill-down — the surface that makes the radar
trustworthy.

A missed important message is a real failure, so for any run the operator must be
able to answer *why* each message was or wasn't promoted. After #7 every run
persists to ``review_runs`` (mode, params, funnel counters) and one
``analysis_trace`` row per (run, chat) capturing the full decision record — input
delta, Stage-1 keyword roots, the exact LLM system+user prompts, the raw model
response, the parsed verdict, the final action, and the Telegram text.

This router is **read-only** (SELECT only, no writes, no cursor changes) so it is
safe on the request path. It exposes a run list (with the funnel), a bounded
cross-run list of filtered decisions, and a per-run drill-down (the per-chat
trace). It never triggers scans — that is the Execution tab (#11).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.webapp.routers._helpers import get_conn
from src.db import store

router = APIRouter()

# sync_log sources that represent data-maintenance runs (not review/scan runs).
# Surfaced in the audit timeline so resync/reprocess are visible alongside runs;
# 'scan'-sourced syncs are omitted because scans already appear as review_runs.
_MAINTENANCE_SOURCES = {"resync", "reprocess"}


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
    """Shape a run row for the run list: identity + parsed params + funnel.

    Since #163 the table holds every run kind; family checks (traffic-check /
    calendar-scan) carry their structured payload in ``summary`` instead of a
    meaningful funnel.
    """
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "summary": _loads(row["summary_json"]),
        "mode": row["mode"],
        "status": row["status"],
        "params": _loads(row["params_json"]),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "notification_status": row["notification_status"],
        "error": row["error"],
        "sources": _loads(row["source_funnel_json"]) or {},
        "funnel": {
            "chats_synced": int(row["chats_synced"]),
            "messages_synced": int(row["messages_synced"]),
            "chats_monitored": int(row["chats_monitored"]),
            "chats_reviewed": int(row["chats_reviewed"]),
            "transcriptions": int(row["transcriptions"]),
            "stage1_passed": int(row["stage1_passed"]),
            "stage2_llm_calls": int(row["stage2_llm_calls"]),
            "actionable": int(row["actionable"]),
        },
    }


def _coverage_gap(
    offline_runs: list[sqlite3.Row], recovery: sqlite3.Row | None
) -> dict[str, Any] | None:
    """Collapse a multi-run connector outage into one Audit timeline marker."""
    if len(offline_runs) < 2:
        return None
    first = offline_runs[0]
    last = offline_runs[-1]
    elapsed = datetime.fromisoformat(last["started_at"]) - datetime.fromisoformat(
        first["started_at"]
    )
    return {
        "started_at": first["started_at"],
        "ended_at": last["started_at"],
        "duration_days": max(1, ceil(elapsed.total_seconds() / 86_400)),
        "failed_runs": len(offline_runs),
        "run_ids": [int(row["id"]) for row in offline_runs],
        "recovered_at": recovery["started_at"] if recovery is not None else None,
        "recovery_run_id": int(recovery["id"]) if recovery is not None else None,
    }


def _coverage_gaps(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Return contiguous multi-run offline windows, newest first."""
    gaps: list[dict[str, Any]] = []
    offline_runs: list[sqlite3.Row] = []
    for row in rows:
        offline = row["status"] == "failed" and row["notification_status"] == "offline"
        if offline:
            offline_runs.append(row)
            continue
        gap = _coverage_gap(offline_runs, row)
        if gap is not None:
            gaps.append(gap)
        offline_runs = []
    gap = _coverage_gap(offline_runs, None)
    if gap is not None:
        gaps.append(gap)
    return list(reversed(gaps))


def _trace_row(row: sqlite3.Row) -> dict[str, Any]:
    """Shape one analysis_trace row (joined to chat name) into the full decision record."""
    return {
        "chat_id": int(row["chat_id"]),
        "source": row["source"],
        "display_name": row["display_name"],
        "input_message_ids": _loads(row["input_message_ids_json"]),
        "input_text": row["input_text"],
        "messages": _loads(row["messages_json"]),
        "stage1_passed": bool(row["stage1_passed"]),
        "stage1_roots": _loads(row["stage1_roots_json"]),
        "stage1_buckets": _loads(row["stage1_buckets_json"]),
        "llm_called": bool(row["llm_called"]),
        "llm_system_prompt": row["llm_system_prompt"],
        "llm_user_prompt": row["llm_user_prompt"],
        "llm_raw_response": row["llm_raw_response"],
        "parsed_result": _loads(row["parsed_result_json"]),
        "final_action": row["final_action"],
        "telegram_text": row["telegram_text"],
        "error": row["error"],
    }


def _filtered_trace_row(row: sqlite3.Row) -> dict[str, Any]:
    """Shape one cross-run filtered item without duplicating the full trace."""
    return {
        "trace_id": int(row["trace_id"]),
        "run_id": int(row["run_id"]),
        "created_at": row["created_at"],
        "source": row["source"],
        "display_name": row["display_name"],
        "stage1_passed": bool(row["stage1_passed"]),
        "stage1_roots": _loads(row["stage1_roots_json"]),
        "llm_called": bool(row["llm_called"]),
        "parsed_result": _loads(row["parsed_result_json"]),
        "final_action": row["final_action"],
    }


@router.get("/api/audit/runs")
async def list_runs(
    limit: int = 50,
    kind: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Recent runs of every kind (with funnel/summary) plus maintenance markers.

    ``runs`` are the inspectable runs, newest first — message scans, process
    runs, and the family checks alike (#163); ``kind`` filters to one kind.
    ``syncs`` are the resync/reprocess data-maintenance events so they're
    visible in the same audit timeline (read-only, no schema beyond sync_log).
    """
    limit = max(1, min(limit, 200))
    runs = [
        r
        for r in (_run_list_row(row) for row in store.list_review_runs(conn, limit))
        if kind is None or r["kind"] == kind
    ]
    syncs = [
        {
            "ran_at": s["ran_at"],
            "source": s["source"],
            "chats_added": int(s["chats_added"]),
            "chats_updated": int(s["chats_updated"]),
            "messages_added": int(s["messages_added"]),
        }
        for s in store.recent_syncs(conn, limit)
        if s["source"] in _MAINTENANCE_SOURCES
    ]
    coverage_gaps = _coverage_gaps(store.list_live_scan_runs(conn))
    return {"runs": runs, "syncs": syncs, "coverage_gaps": coverage_gaps}


@router.get("/api/audit/filtered")
async def list_filtered(
    days: int = 30,
    limit: int = 50,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Recent cross-run decisions that did not produce an actionable item."""
    days = max(1, min(days, 3650))
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    total = store.count_filtered_traces(conn, since)
    items = [
        _filtered_trace_row(row)
        for row in store.list_filtered_traces(conn, since, limit, offset)
    ]
    return {
        "days": days,
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_more": offset + len(items) < total,
        "items": items,
    }


@router.get("/api/audit/runs/{run_id}")
async def get_run(
    run_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """One run's header + funnel and its per-chat audit trace (the drill-down)."""
    run = store.review_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    traces = [_trace_row(t) for t in store.traces_for_run(conn, run_id)]
    return {"run": _run_list_row(run), "traces": traces}
