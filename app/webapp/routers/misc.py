"""Catch-all routes: index, healthz, version."""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import subprocess
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.webapp.routers._helpers import STATIC_DIR
from src.paths import PROJECT_ROOT
from src.static_versioning import asset_hash_for, inline_index_partials, rewrite_index_html
from src.subprocess_flags import NO_WINDOW

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
        creationflags=NO_WINDOW,
    )
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


def _entry_etag(stamped: str, asset_hashes: dict[str, str]) -> str:
    """Weak validator over the build fingerprint, not just the served body.

    The stamped HTML names only the directly referenced assets, so a changed
    nested module can leave it byte-identical; folding in the git sha and every
    asset hash keeps a 304 from vouching for an out-of-date build. Weak because
    the wire bytes vary with ``Content-Encoding``.
    """
    digest = hashlib.sha256()
    digest.update(_GIT_SHA.encode("utf-8"))
    for name in sorted(asset_hashes):
        digest.update(f"\n{name}:{asset_hashes[name]}".encode())
    digest.update(b"\n")
    digest.update(stamped.encode("utf-8"))
    return f'W/"{digest.hexdigest()[:16]}"'


def _if_none_match_hits(header: str | None, etag: str) -> bool:
    """RFC 9110 weak comparison: ``*`` matches, and ``W/`` is ignored on both sides."""
    if not header:
        return False
    wanted = etag.removeprefix("W/")
    return any(
        candidate in ("*", wanted)
        for candidate in (part.strip().removeprefix("W/") for part in header.split(","))
    )


@router.get("/")
async def index(request: Request) -> Response:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="index.html missing")
    asset_hashes = getattr(request.app.state, "asset_hashes", {}) or {}
    body = inline_index_partials(index_path.read_text(encoding="utf-8"), STATIC_DIR)
    stamped = rewrite_index_html(body, asset_hashes)
    etag = _entry_etag(stamped, asset_hashes)
    # Force Safari (iPhone PWA especially) to revalidate the HTML on every load
    # so a stale cached index can't keep pointing at a `?v=<old hash>` script;
    # the ETag lets that revalidation answer 304 instead of re-sending the body.
    headers = {"Cache-Control": "no-cache, must-revalidate", "ETag": etag}
    if _if_none_match_hits(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return HTMLResponse(content=stamped, headers=headers)


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
