"""Dashboard metrics — read-only aggregates over the local SQLite store.

The at-a-glance health view: how many channels are watched, how much has been
stored, how many scans ran, what's waiting since the last scan, and how many
real alerts the radar has raised. Everything here is SELECT-only — no writes,
no cursor changes — so it is safe on the request path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from src.config import load_config
from src.db import store

router = APIRouter()


def _db_path(request: Request) -> Path:
    # Tests/e2e inject a fixture DB via app.state.db_path; fall back to config.
    path = getattr(request.app.state, "db_path", None)
    return Path(path) if path is not None else load_config().db_path


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


@router.get("/api/dashboard")
async def dashboard(request: Request) -> dict[str, Any]:
    conn = store.connect(_db_path(request))
    try:
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
            "alerts": {
                "actionable": store.count_actionable_items(conn),
                "notifications_sent": store.count_notifications_sent(conn),
            },
        }
    finally:
        conn.close()
