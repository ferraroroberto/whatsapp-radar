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
_ALIAS_MAX = 100


def _db_path(request: Request) -> Path:
    # Tests/e2e inject a fixture DB via app.state.db_path; fall back to config.
    path = getattr(request.app.state, "db_path", None)
    return Path(path) if path is not None else load_config().db_path


class StatusUpdate(BaseModel):
    status: str


class AliasUpdate(BaseModel):
    # An empty/whitespace value clears the alias (falls back to the derived name).
    alias: str | None = None


class LinkUpdate(BaseModel):
    # The canonical (top-level) chat this chat should be folded into as a child.
    parent_id: int


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
                    "alias": row["alias"],
                    "type": row["chat_type"],
                    "status": row["status"],
                    "count": int(row["message_count"]),
                    "last_message_at": row["last_message_at"],
                    "last_message_text": row["last_message_text"],
                    # The parent link: present (non-null) on a child chat the
                    # operator has folded into another. The frontend hides children
                    # from the list and nests them under their parent.
                    "parent_chat_id": row["parent_chat_id"],
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
        # A parent's history is the time-ordered merge of itself and its linked
        # children; for a standalone or child chat this is just its own messages.
        member_ids = store.family_member_ids(conn, chat_id)
        multi = len(member_ids) > 1
        # Per-origin labels so each message in a merged family stays attributable.
        origin: dict[int, str] = {}
        if multi:
            for mid in member_ids:
                row = store.get_chat(conn, mid)
                if row is not None:
                    origin[mid] = row["alias"] or row["display_name"]
        messages, has_more = store.recent_messages_family(
            conn, member_ids, limit=limit, before_ts=before_ts, before_id=before_id
        )
        return {
            "chat_id": chat_id,
            "name": chat["display_name"],
            "alias": chat["alias"],
            "has_more": has_more,
            "messages": [
                {
                    "id": m.id,
                    "ts": m.message_timestamp,
                    "sender": m.sender_label,
                    "text": m.text,
                    "type": m.message_type,
                    # Only on a merged family view (>1 member); absent for a lone chat.
                    **({"origin": origin.get(m.chat_id)} if multi else {}),
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


@router.post("/api/chats/{chat_id}/alias")
async def set_alias(request: Request, chat_id: int, payload: AliasUpdate) -> dict[str, Any]:
    cleaned = (payload.alias or "").strip()[:_ALIAS_MAX] or None
    conn = store.connect(_db_path(request))
    try:
        if store.get_chat(conn, chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        store.set_chat_alias(conn, chat_id, cleaned)
        return {"id": chat_id, "alias": cleaned}
    finally:
        conn.close()


@router.post("/api/chats/{chat_id}/link")
async def link_chat(request: Request, chat_id: int, payload: LinkUpdate) -> dict[str, Any]:
    """Link ``chat_id`` as a child of ``payload.parent_id`` (also re-parents).

    The link is keyed on the child. Returns 404 if either chat is missing and 400
    if the link would break the depth-1 rules (self-link, linking under a child,
    or linking a chat that already has children).
    """
    conn = store.connect(_db_path(request))
    try:
        if store.get_chat(conn, chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        if store.get_chat(conn, payload.parent_id) is None:
            raise HTTPException(status_code=404, detail="parent chat not found")
        try:
            store.link_chats(conn, chat_id, payload.parent_id)
        except store.LinkError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": chat_id, "parent_id": payload.parent_id}
    finally:
        conn.close()


@router.post("/api/chats/{chat_id}/unlink")
async def unlink_chat(request: Request, chat_id: int) -> dict[str, Any]:
    """Remove ``chat_id``'s parent link, restoring it as an independent chat.

    Used both to detach a child and to unlink one child from a parent's overlay
    (the call targets the child either way). No message data or cursor is touched.
    """
    conn = store.connect(_db_path(request))
    try:
        if store.get_chat(conn, chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        unlinked = store.unlink_chat(conn, chat_id)
        return {"id": chat_id, "unlinked": unlinked}
    finally:
        conn.close()
