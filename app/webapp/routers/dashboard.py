"""Dashboard metrics — read-only aggregates over the local SQLite store.

The at-a-glance health view: how many channels are watched, how much has been
stored, how many scans ran, what's waiting since the last scan, and how many
real alerts the radar has raised. Everything here is SELECT-only — no writes,
no cursor changes — so it is safe on the request path.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends

from app.webapp.routers._helpers import get_conn
from src.db import store

router = APIRouter()

# Message-pipeline kinds carry the per-source scan funnel; the family checks
# (traffic-check / calendar-scan) carry a structured summary payload instead.
_MESSAGE_KINDS = ("scan", "process")


def _run_summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "mode": row["mode"],
        "status": row["status"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "messages_synced": int(row["messages_synced"]),
        "chats_reviewed": int(row["chats_reviewed"]),
        "actionable": int(row["actionable"]),
        "notification_status": row["notification_status"],
    }


def _loads(value: Any) -> Any:
    """Parse a stored JSON column, tolerating null/blank/legacy non-JSON text."""
    if not value or not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _never() -> dict[str, Any]:
    return {"kind": None, "db_run_id": None, "started_at": None,
            "status": None, "alerts": 0, "summary": ""}


def _message_activity(runs: list[sqlite3.Row], source: str) -> dict[str, Any]:
    """Last-activity card for one message source (WhatsApp / Gmail).

    Uses the most recent scan/process run that actually included this source, so
    a card reflects when the source last ran — not a later run that skipped it
    (e.g. Gmail disabled). The distilled line is "N new · M actionable".
    """
    for row in runs:
        if row["kind"] not in _MESSAGE_KINDS:
            continue
        funnel = (_loads(row["source_funnel_json"]) or {}).get(source)
        if funnel is None:
            continue
        synced = int(funnel.get("messages_synced") or 0)
        actionable = int(funnel.get("actionable") or 0)
        return {
            "kind": row["kind"],
            "db_run_id": int(row["id"]),
            "started_at": row["started_at"],
            "status": row["status"],
            "alerts": actionable,
            "summary": f"{synced} new · {actionable} actionable",
        }
    return _never()


def _traffic_activity(runs: list[sqlite3.Row]) -> dict[str, Any]:
    """Last-activity card for the traffic-jam check."""
    row = next((r for r in runs if r["kind"] == "traffic-check"), None)
    if row is None:
        return _never()
    result = _loads(row["summary_json"]) or {}
    alerts = int(result.get("alerts") or 0)
    checked = len(result.get("checked") or [])
    rstatus = result.get("status")
    if rstatus == "disabled":
        summary = "checks disabled"
    elif rstatus == "quiet_hours":
        summary = "quiet hours"
    elif alerts:
        summary = f"{alerts} delay alert" + ("" if alerts == 1 else "s")
    elif checked:
        summary = "no significant delay"
    else:
        summary = "no commutes to check"
    return {
        "kind": row["kind"],
        "db_run_id": int(row["id"]),
        "started_at": row["started_at"],
        "status": row["status"],
        "alerts": alerts,
        "summary": summary,
    }


def _calendar_activity(runs: list[sqlite3.Row]) -> dict[str, Any]:
    """Last-activity card for the daily calendar-conflict scan."""
    row = next((r for r in runs if r["kind"] == "calendar-scan"), None)
    if row is None:
        return _never()
    result = _loads(row["summary_json"]) or {}
    conflicts = len(result.get("conflicts") or [])
    # Renamed unknown_locations -> missing_locations in #168; old rows persist.
    missing = len(result.get("missing_locations") or result.get("unknown_locations") or [])
    alerts = conflicts + missing
    if result.get("status") == "disabled":
        summary, alerts = "scan disabled", 0
    elif alerts:
        summary = f"{conflicts} conflict" + ("" if conflicts == 1 else "s") + \
            f" · {missing} missing location" + ("" if missing == 1 else "s")
    else:
        summary = "no conflicts"
    return {
        "kind": row["kind"],
        "db_run_id": int(row["id"]),
        "started_at": row["started_at"],
        "status": row["status"],
        "alerts": alerts,
        "summary": summary,
    }


def _last_activity(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One "last activity" card per kind (WhatsApp · Gmail · Traffic · Calendar).

    Each answers "when did this last run and what did it find" from the unified
    run store (#163), so a CLI- or Jobs-launched run is as visible here as a
    webapp one. ``db_run_id`` lets the Dashboard deep-link to the Run tab detail.
    """
    runs = store.list_review_runs(conn, 200)
    return [
        {"source": "whatsapp", **_message_activity(runs, "whatsapp")},
        {"source": "gmail", **_message_activity(runs, "gmail")},
        {"source": "traffic", **_traffic_activity(runs)},
        {"source": "calendar", **_calendar_activity(runs)},
    ]


@router.get("/api/dashboard")
async def dashboard(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    chats = store.count_chats_by_status(conn)
    per_chat = store.messages_per_chat(conn, monitored_only=True)
    last = store.last_run(conn)
    backlog = store.count_messages_since(conn, last["started_at"]) if last else 0
    return {
        "chats": {**chats, "total": sum(chats.values())},
        "messages": {
            "total": store.message_count_total(conn),
            "per_channel": [
                {
                    "chat_id": int(row["id"]),
                    "source": row["source"],
                    "name": row["display_name"],
                    "status": row["status"],
                    "count": int(row["message_count"]),
                    "last_message_at": row["last_message_at"],
                }
                for row in per_chat
            ],
        },
        "scans": {
            "count": store.count_runs(conn),
            "messages_since_last": backlog,
            "last": _run_summary(last),
        },
        "last_activity": _last_activity(conn),
        "alerts": {
            "actionable": store.count_actionable_items(conn),
            "notifications_sent": store.count_notifications_sent(conn),
        },
        "sources": [
            {
                "source": row["source"],
                "channels": int(row["channels"]),
                "monitored": int(row["monitored"]),
                "messages": int(row["messages"]),
                "latest_message_at": row["latest_message_at"],
            }
            for row in store.source_overview(conn)
        ],
    }
