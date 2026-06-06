"""Chats tab (#10): chat selection, history overlay, monitor/ignore toggle.

Listing and history are read-only SELECTs over the local store. The only writes
are status changes, which go through ``store.set_chat_status`` — and marking a
chat *monitored* also baselines its review cursor (``store.baseline_cursor``) so
the first review classifies only *new* messages, never months of backlog.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.config import load_config
from src.db import store

router = APIRouter()

_VALID_STATUSES = {"discovered", "monitored", "ignored"}
_HISTORY_MAX = 200


def _db_path(request: Request) -> Path:
    # Tests/e2e inject a fixture DB via app.state.db_path; fall back to config.
    path = getattr(request.app.state, "db_path", None)
    return Path(path) if path is not None else load_config().db_path


class StatusUpdate(BaseModel):
    status: str


@router.get("/api/chats")
async def list_chats(request: Request) -> dict[str, Any]:
    conn = store.connect(_db_path(request))
    try:
        rows = store.chats_overview(conn)
        return {
            "chats": [
                {
                    "id": int(row["id"]),
                    "source_chat_id": row["source_chat_id"],
                    "name": row["display_name"],
                    "type": row["chat_type"],
                    "status": row["status"],
                    "count": int(row["message_count"]),
                    "last_message_at": row["last_message_at"],
                    "last_message_text": row["last_message_text"],
                }
                for row in rows
            ]
        }
    finally:
        conn.close()


@router.get("/api/chats/{chat_id}/history")
async def chat_history(
    request: Request,
    chat_id: int,
    limit: int = 30,
    before_ts: str | None = None,
    before_id: int | None = None,
) -> dict[str, Any]:
    limit = max(1, min(limit, _HISTORY_MAX))
    conn = store.connect(_db_path(request))
    try:
        chat = store.get_chat(conn, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="chat not found")
        messages, has_more = store.recent_messages(
            conn, chat_id, limit=limit, before_ts=before_ts, before_id=before_id
        )
        return {
            "chat_id": chat_id,
            "name": chat["display_name"],
            "has_more": has_more,
            "messages": [
                {
                    "id": m.id,
                    "ts": m.message_timestamp,
                    "sender": m.sender_label,
                    "text": m.text,
                    "type": m.message_type,
                }
                for m in messages
            ],
        }
    finally:
        conn.close()


@router.post("/api/chats/{chat_id}/status")
async def set_status(request: Request, chat_id: int, payload: StatusUpdate) -> dict[str, Any]:
    if payload.status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid status {payload.status!r} (expected one of {sorted(_VALID_STATUSES)})",
        )
    conn = store.connect(_db_path(request))
    try:
        if store.get_chat(conn, chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        store.set_chat_status(conn, chat_id, payload.status)
        # Baselining only happens the first time a chat is monitored (no-op if it
        # already has a cursor or no messages), so re-monitoring never re-baselines.
        baselined = (
            store.baseline_cursor(conn, chat_id) if payload.status == "monitored" else False
        )
        return {"id": chat_id, "status": payload.status, "baselined": baselined}
    finally:
        conn.close()
