"""Catch-all routes: index, healthz, version, install-ca."""

from __future__ import annotations

import datetime as _dt
import logging
import subprocess
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.webapp.routers._helpers import PROJECT_ROOT, STATIC_DIR
from src.static_versioning import asset_hash_for, rewrite_index_html

_log = logging.getLogger(__name__)

router = APIRouter()


def _resolve_git_sha() -> str:
    """Short git SHA, captured once at module import.

    Falls back to ``"unknown"`` if git isn't on PATH or this isn't a repo. The
    pythonw tray has no console, so ``CREATE_NO_WINDOW`` keeps a stray cmd from
    flashing and ``stdin=DEVNULL`` avoids the invalid-handle trap.
    """
    cmd = ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"]
    kwargs: dict[str, Any] = dict(
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
    )
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(cmd, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("⚠️ /api/version: git rev-parse raised %s: %s", type(exc).__name__, exc)
        return "unknown"
    sha = (result.stdout or "").strip()
    if not sha:
        _log.warning(
            "⚠️ /api/version: git rev-parse exit=%s stderr=%r",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return "unknown"
    return sha


_GIT_SHA = _resolve_git_sha()
_BUILT_AT = _dt.datetime.now().replace(microsecond=0).isoformat()


@router.get("/")
async def index(request: Request) -> HTMLResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="index.html missing")
    asset_hashes = getattr(request.app.state, "asset_hashes", {}) or {}
    body = index_path.read_text(encoding="utf-8")
    stamped = rewrite_index_html(body, asset_hashes)
    # Force Safari (iPhone PWA especially) to revalidate the HTML on every load
    # so a stale cached index can't keep pointing at a `?v=<old hash>` script.
    return HTMLResponse(
        content=stamped,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@router.get("/api/version")
async def version(request: Request) -> dict[str, str]:
    """Build identity. Stable across requests; cached at module load."""
    asset_hashes = getattr(request.app.state, "asset_hashes", {}) or {}
    return {
        "git_sha": _GIT_SHA,
        "built_at": _BUILT_AT,
        "asset_hash": asset_hash_for(asset_hashes, "styles.css") or "",
    }


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "service": "whatsapp-radar"}


@router.get("/install-ca")
async def install_ca() -> FileResponse:
    profile = STATIC_DIR / "whatsapp-radar-ca.mobileconfig"
    if not profile.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "CA profile not generated yet. Run "
                "`scripts/gen_ssl_cert.py` from the project root."
            ),
        )
    return FileResponse(
        str(profile),
        media_type="application/x-apple-aspen-config",
        filename="whatsapp-radar-ca.mobileconfig",
    )
