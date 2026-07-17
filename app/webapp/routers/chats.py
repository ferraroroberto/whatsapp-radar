"""Chats tab (#10): chat selection, history overlay, monitor/ignore toggle.

Listing and history are read-only SELECTs over the local store. The only writes
are status changes, which go through ``store.set_chat_status`` — and marking a
chat *monitored* also baselines its review cursor (``store.baseline_cursor``) so
the first review classifies only *new* messages, never months of backlog.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from app.webapp.routers._helpers import buffer_dir, db_path, get_conn, hub_base_url
from src import tts_client
from src.analysis import summarize as summarize_client
from src.db import store

logger = logging.getLogger(__name__)

router = APIRouter()

# Minimum characters of message text before a summary is worth a hub call. Mirrors
# the frontend's SUMMARIZE_MIN_CHARS in chats.js (the button only shows past this);
# the backend re-checks so a hand-crafted request can't burn a hub call on a one-liner.
_SUMMARIZE_MIN_CHARS = 280

# Audio content types by extension for the playback endpoint. WhatsApp voice
# notes arrive as OGG/Opus; WAV appears only if a note was transcoded in place.
_AUDIO_MEDIA_TYPES = {".ogg": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav"}


def _transcode_to_mp3(src: Path) -> bytes:
    """Transcode an audio file to mono 24 kHz MP3 bytes via ffmpeg (#86).

    WhatsApp voice notes are OGG/Opus, which iOS Safari's ``<audio>`` element can't
    play; MP3 plays on every target browser. Raises ``FileNotFoundError`` when
    ffmpeg isn't on PATH and ``subprocess.CalledProcessError`` on a transcode error,
    both of which the endpoint catches to fall back to the original bytes.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("ffmpeg not on PATH")
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "24000", "-b:a", "64k", "-f", "mp3", "pipe:1"],
        capture_output=True, check=True,
    )
    return proc.stdout

_VALID_STATUSES = {"discovered", "monitored", "ignored"}
_HISTORY_MAX = 200
_ALIAS_MAX = 100


class StatusUpdate(BaseModel):
    status: str


class AliasUpdate(BaseModel):
    # An empty/whitespace value clears the alias (falls back to the derived name).
    alias: str | None = None


class LinkUpdate(BaseModel):
    # The canonical (top-level) chat this chat should be folded into as a child.
    parent_id: int


class SpeechRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: float | None = None


@router.get("/api/chats")
async def list_chats(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = store.chats_overview(conn)
    return {
        "chats": [
            {
                "id": int(row["id"]),
                "source": row["source"],
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


@router.get("/api/chats/{chat_id}/history")
async def chat_history(
    chat_id: int,
    limit: int = 30,
    before_ts: str | None = None,
    before_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    limit = max(1, min(limit, _HISTORY_MAX))
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
    source_by_chat = {
        mid: str(row["source"])
        for mid in member_ids
        if (row := store.get_chat(conn, mid)) is not None
    }

    def email_meta(message: Any) -> dict[str, Any]:
        if source_by_chat.get(message.chat_id) != "gmail":
            return {}
        headers = message.raw.get("headers") or {}
        subject = headers.get("Subject") or headers.get("subject")
        return {
            "subject": str(subject or "(no subject)"),
            "thread_id": message.raw.get("thread_id"),
        }

    return {
        "chat_id": chat_id,
        "source": chat["source"],
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
                "source": source_by_chat.get(m.chat_id, str(chat["source"])),
                # Voice-note transcription state (#36) so the UI can mark a voice
                # note and label it when it isn't (yet) transcribed.
                "transcription_status": m.transcription_status,
                # True when this voice note still has retained audio on disk, so
                # the overlay can offer a play control wired to the audio endpoint
                # (#86). False once the audio is swept past the retention window.
                "has_audio": m.message_type == "voice" and m.media_path is not None,
                # Only on a merged family view (>1 member); absent for a lone chat.
                **({"origin": origin.get(m.chat_id)} if multi else {}),
                **email_meta(m),
            }
            for m in messages
        ],
    }


@router.post("/api/chats/{chat_id}/status")
async def set_status(
    chat_id: int,
    payload: StatusUpdate,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if payload.status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid status {payload.status!r} (expected one of {sorted(_VALID_STATUSES)})",
        )
    if store.get_chat(conn, chat_id) is None:
        raise HTTPException(status_code=404, detail="chat not found")
    store.set_chat_status(conn, chat_id, payload.status)
    # Baselining only happens the first time a chat is monitored (no-op if it
    # already has a cursor or no messages), so re-monitoring never re-baselines.
    baselined = (
        store.baseline_cursor(conn, chat_id) if payload.status == "monitored" else False
    )
    return {"id": chat_id, "status": payload.status, "baselined": baselined}


@router.post("/api/chats/{chat_id}/alias")
async def set_alias(
    chat_id: int,
    payload: AliasUpdate,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    cleaned = (payload.alias or "").strip()[:_ALIAS_MAX] or None
    if store.get_chat(conn, chat_id) is None:
        raise HTTPException(status_code=404, detail="chat not found")
    store.set_chat_alias(conn, chat_id, cleaned)
    return {"id": chat_id, "alias": cleaned}


@router.post("/api/chats/{chat_id}/link")
async def link_chat(
    chat_id: int,
    payload: LinkUpdate,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Link ``chat_id`` as a child of ``payload.parent_id`` (also re-parents).

    The link is keyed on the child. Returns 404 if either chat is missing and 400
    if the link would break the depth-1 rules (self-link, linking under a child,
    or linking a chat that already has children).
    """
    if store.get_chat(conn, chat_id) is None:
        raise HTTPException(status_code=404, detail="chat not found")
    if store.get_chat(conn, payload.parent_id) is None:
        raise HTTPException(status_code=404, detail="parent chat not found")
    try:
        store.link_chats(conn, chat_id, payload.parent_id)
    except store.LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": chat_id, "parent_id": payload.parent_id}


@router.post("/api/chats/{chat_id}/unlink")
async def unlink_chat(
    chat_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Remove ``chat_id``'s parent link, restoring it as an independent chat.

    Used both to detach a child and to unlink one child from a parent's overlay
    (the call targets the child either way). No message data or cursor is touched.
    """
    if store.get_chat(conn, chat_id) is None:
        raise HTTPException(status_code=404, detail="chat not found")
    unlinked = store.unlink_chat(conn, chat_id)
    return {"id": chat_id, "unlinked": unlinked}


@router.get("/api/messages/{message_id}/audio")
async def message_audio(
    request: Request,
    message_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """Stream a voice note's retained audio for playback in the Chats overlay (#86).

    Read-only and gated by the same bearer-token middleware as the rest of the API
    (the ``<audio>`` element passes the token via ``?token=``; loopback bypasses).
    404s cleanly when the message has no retained audio — not a voice note, audio
    never downloaded, or swept past the retention window. The served path is
    confined to the linked-device buffer dir: a ``media_path`` that resolves outside
    it (traversal) is refused.

    WhatsApp voice notes are OGG/Opus, which iOS Safari can't play in an ``<audio>``
    element, so anything non-WAV is transcoded to MP3 on the fly for universal
    playback. If ffmpeg is unavailable or fails, the original file is served as a
    fallback (still plays on Chrome/Android/desktop). WAV is passed through with
    Range support so the player can seek.
    """
    media_path = store.voice_audio_path(conn, message_id)
    if not media_path:
        raise HTTPException(status_code=404, detail="no audio for this message")

    base = buffer_dir(request).resolve()
    target = (base / media_path).resolve()
    if not target.is_relative_to(base):
        raise HTTPException(status_code=404, detail="no audio for this message")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="audio file not found")

    suffix = target.suffix.lower()
    if suffix == ".wav":
        return FileResponse(target, media_type="audio/wav")
    try:
        mp3 = await asyncio.to_thread(_transcode_to_mp3, target)
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("⚠️ audio transcode failed for message %s: %s", message_id, exc)
        media_type = _AUDIO_MEDIA_TYPES.get(suffix, "application/octet-stream")
        return FileResponse(target, media_type=media_type)
    return Response(content=mp3, media_type="audio/mpeg")


@router.post("/api/messages/{message_id}/summarize")
async def summarize_message(request: Request, message_id: int) -> dict[str, Any]:
    """Summarize one long message's text on demand via the hub's Haiku (#86).

    The Chats overlay shows a Summarize control on any message past
    :data:`_SUMMARIZE_MIN_CHARS`; this condenses it to its essence plus any action
    the reader must take. Read-only and gated by the same bearer-token middleware
    as the rest of the API. The summary is **ephemeral** — computed per click,
    never stored — so no schema change and nothing extra committed.

    404 when the message is missing or has no text (e.g. an untranscribed voice
    note); 400 when the text is too short to be worth a hub call; the hub's own
    status (503 unreachable / upstream error / 502 empty) is surfaced verbatim.
    """
    conn = store.connect(db_path(request))
    try:
        text = store.message_text(conn, message_id)
    finally:
        conn.close()
    if text is None:
        raise HTTPException(status_code=404, detail="no text for this message")
    if len(text) < _SUMMARIZE_MIN_CHARS:
        raise HTTPException(status_code=400, detail="message is too short to summarize")

    # Injectable so the offline suite never dials the hub; production uses the
    # real hub-backed client bound to the configured :8000 base.
    summarizer = getattr(request.app.state, "summarizer", None) or summarize_client.summarize
    base = hub_base_url(request)
    try:
        summary = await asyncio.to_thread(summarizer, base, text)
    except summarize_client.SummarizeError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"message_id": message_id, "summary": summary}


@router.get("/api/tts/health")
async def tts_health(request: Request) -> dict[str, bool]:
    """Report whether the hub is reachable for manual summary playback (#94)."""
    probe = getattr(request.app.state, "tts_health", None) or tts_client.health
    try:
        available = await asyncio.to_thread(probe, hub_base_url(request))
    except tts_client.TtsError as exc:
        logger.info("ℹ️ summary TTS health probe unavailable: %s", exc)
        return {"available": False}
    return {"available": bool(available)}


@router.post("/api/tts/speak")
async def tts_speak(request: Request, speech: SpeechRequest) -> StreamingResponse:
    """Proxy one summary to the hub as a headerless PCM16 stream (#94).

    The existing bearer middleware gates this route like every other ``/api``
    endpoint. Audio stays ephemeral: the bytes are forwarded as they arrive and
    are never written to disk or retained by the application.
    """
    text = speech.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")

    payload = tts_client.build_speech_payload(
        text,
        voice=speech.voice,
        speed=speech.speed,
    )
    upstream_url = tts_client.speech_url(hub_base_url(request))
    client = httpx.AsyncClient(timeout=None)
    stream_cm = client.stream("POST", upstream_url, json=payload)
    try:
        upstream = await stream_cm.__aenter__()
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("⚠️ summary TTS upstream connection failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="text-to-speech service is unavailable"
        ) from exc
    if upstream.status_code >= 400:
        await upstream.aread()
        await stream_cm.__aexit__(None, None, None)
        await client.aclose()
        logger.warning("⚠️ summary TTS upstream returned HTTP %s", upstream.status_code)
        raise HTTPException(status_code=502, detail="text-to-speech request failed")

    sample_rate = upstream.headers.get("x-sample-rate", "24000")

    async def forward() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                if chunk:
                    yield chunk
        except httpx.HTTPError as exc:
            logger.warning("⚠️ summary TTS stream ended early: %s", exc)
        finally:
            await stream_cm.__aexit__(None, None, None)
            await client.aclose()

    return StreamingResponse(
        forward(),
        media_type="audio/L16",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Sample-Rate": str(sample_rate),
        },
    )
