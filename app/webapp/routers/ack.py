"""Follow-ups tab (#219): the non-routine acknowledgment surface.

A non-routine prep item (Step 5/5 of #206) gets a distinct, acknowledgeable
follow-up here, alongside — never instead of — the existing Telegram alert, so
it doesn't blend into routine reminders and get forgotten. Minimal surface:
list pending items, acknowledge one. No general done/snooze/ack tooling for
the whole obligation list (that's epic #15).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.webapp.routers._helpers import get_conn
from src.db import store

router = APIRouter()


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "run_id": int(row["run_id"]),
        "chat_id": int(row["chat_id"]),
        "display_name": row["display_name"],
        "child": row["child"],
        "task_category": row["task_category"],
        "summary": row["summary"],
        "calendar_event_id": row["calendar_event_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "acknowledged_at": row["acknowledged_at"],
    }


@router.get("/api/ack/items")
async def list_pending(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """Pending non-routine follow-ups, newest first."""
    return {"items": [_row(r) for r in store.pending_ack_items(conn)]}


@router.post("/api/ack/{item_id}/acknowledge")
async def acknowledge(
    item_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Mark one follow-up acknowledged. Idempotent on a repeat tap."""
    if store.get_ack_item(conn, item_id) is None:
        raise HTTPException(status_code=404, detail="ack item not found")
    store.acknowledge_item(conn, item_id)
    return {"id": item_id, "status": "acknowledged"}
