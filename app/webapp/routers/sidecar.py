"""Sidecar control (#29): see the WhatsApp connection state, relaunch it, re-pair.

The phone-first counterpart to the CLI preflight. The Execution tab's health pill
reads :func:`sidecar_status` to colour the dot and, when the source is down,
offers a one-tap relaunch (:func:`start_sidecar`) and — when a fresh QR is needed
— serves the pairing image (:func:`sidecar_qr`) so a non-technical household
member can re-link a device from their phone without ever touching a terminal.

All routes sit under ``/api`` and are covered by the bearer-token middleware
(loopback bypasses). The QR is a local file under the ignored buffer dir; it is
served no-cache because it rotates on each pairing refresh, and never committed.
The sidecar stays read-only — relaunching the Node process is lifecycle control,
not a WhatsApp write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from src.config import load_config
from src.connector import sidecar

router = APIRouter()


def _buffer_dir(request: Request) -> Path:
    # Tests/e2e inject the buffer dir via app.state; fall back to config.
    path = getattr(request.app.state, "linked_device_dir", None)
    return Path(path) if path is not None else load_config().linked_device_dir


@router.get("/api/sidecar/status")
async def sidecar_status(request: Request) -> dict[str, Any]:
    """Coarse lifecycle state of the WhatsApp sidecar, derived from its heartbeat.

    Never raises: a missing/stale sidecar is itself a valid state the UI renders.
    """
    return sidecar.sidecar_state(_buffer_dir(request)).to_dict()


@router.post("/api/sidecar/start")
async def start_sidecar(request: Request) -> dict[str, Any]:
    """(Re)launch the sidecar if it isn't already live; return the resulting state.

    Non-blocking: it spawns the process (or no-ops if one is already running) and
    returns immediately so the UI can poll ``/api/sidecar/status`` while the
    session links. A missing Node runtime / uninstalled deps surface as 503 with
    an actionable message rather than a generic 500.
    """
    buffer_dir = _buffer_dir(request)
    try:
        result = sidecar.launch_sidecar(buffer_dir)
    except sidecar.SidecarLaunchError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    result["state"] = sidecar.sidecar_state(buffer_dir).to_dict()
    return result


@router.get("/api/sidecar/qr")
async def sidecar_qr(request: Request) -> FileResponse:
    """Serve the current pairing QR PNG (no-cache), or 404 when none is pending."""
    qr_path = _buffer_dir(request) / "qr.png"
    if not qr_path.is_file():
        raise HTTPException(status_code=404, detail="no pairing QR available")
    return FileResponse(
        qr_path,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )
