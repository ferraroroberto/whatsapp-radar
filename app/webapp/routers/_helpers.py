"""Cross-router helpers — no router imports another router; shared utility
lives here instead.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Request

from src.config import load_config
from src.db import store

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def maybe_json(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def db_path(request: Request) -> Path:
    """Return the DB path for this request.

    Tests and e2e inject a fixture DB via ``app.state.db_path``; production
    falls back to the loaded config.  Centralised here so a change to the
    override mechanism (e.g. an env-override layer) only touches one place.
    """
    path = getattr(request.app.state, "db_path", None)
    return Path(path) if path is not None else load_config().db_path


async def get_conn(request: Request) -> AsyncIterator[sqlite3.Connection]:
    """FastAPI dependency yielding a store connection scoped to the request.

    The single home for the open/close lifecycle that the read/write handlers
    used to repeat as ``conn = store.connect(db_path(request))`` / ``try: …
    finally: conn.close()``.  The connection *setup* itself (row factory, WAL
    pragmas, schema + migrate) already lives in :func:`src.db.store.connect`;
    this dependency owns only the lifecycle, so handlers inject it with
    ``conn: sqlite3.Connection = Depends(get_conn)`` and the connection is
    closed when the request finishes (including on an ``HTTPException``).

    Deliberately ``async``: every consuming handler is ``async`` and runs in the
    event-loop thread, so the connection must be opened (and closed) on that same
    thread — a sync dependency would run in Starlette's threadpool and trip
    sqlite3's ``check_same_thread`` guard. This matches the prior behaviour, where
    each handler called :func:`store.connect` inline on the event-loop thread.
    """
    conn = store.connect(db_path(request))
    try:
        yield conn
    finally:
        conn.close()


def buffer_dir(request: Request) -> Path:
    """Return the sidecar buffer directory for this request.

    Tests and e2e inject the directory via ``app.state.linked_device_dir``;
    production falls back to the loaded config.  Same override pattern as
    :func:`db_path`.
    """
    path = getattr(request.app.state, "linked_device_dir", None)
    return Path(path) if path is not None else load_config().linked_device_dir


def hub_base_url(request: Request) -> str:
    """Return the local-llm-hub base URL for this request (#86 summarize).

    Tests inject an override via ``app.state.hub_base_url`` (and the summarize
    endpoint also injects a fake summarizer, so the URL is never dialled in the
    offline suite); production falls back to the loaded config's hub block — the
    same ``:8000`` proxy the classifier and transcription already use.
    """
    base = getattr(request.app.state, "hub_base_url", None)
    return str(base) if base is not None else load_config().hub.base_url
