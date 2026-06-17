"""Cross-router helpers — no router imports another router; shared utility
lives here instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request

from src.config import load_config

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


def cert_present() -> bool:
    return (
        (PROJECT_ROOT / "webapp" / "certificates" / "cert.pem").exists()
        and (PROJECT_ROOT / "webapp" / "certificates" / "key.pem").exists()
    )


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
