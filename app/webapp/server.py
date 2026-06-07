"""FastAPI webapp — phone-first admin hub for WhatsApp Radar.

Routes (split across ``app/webapp/routers/``):

    GET  /                       → static/index.html              (misc)
    GET  /static/{file}          → CSS / JS / icons               (static mount)
    GET  /healthz                → liveness probe                 (misc)
    GET  /api/version            → git sha + asset hash           (misc)
    GET  /install-ca             → iOS .mobileconfig              (misc)
    POST /api/login              → swap password for token        (auth)
    /api/webauthn/*              → passkey ceremonies             (webauthn)
    GET  /api/dashboard          → read-only metrics              (dashboard)
    /api/chats[...]             → list / history / status toggle (chats)
    GET/POST /api/config         → prompt + safe settings         (config)
    /api/execution/*            → run pipeline pieces + run log   (execution)
    /api/sidecar/*              → WhatsApp connection state/QR    (sidecar)

Dashboard (#9), Chats & Config (#10) and Execution (#11) are live; Audit is
still an empty shell that Step 7 (#12) fills. The sidecar routes (#29) back the
Execution health pill's relaunch / re-pair affordances.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from app.webapp.middleware import BearerTokenMiddleware
from app.webapp.routers import audit, auth, chats, dashboard, execution, misc, sidecar, webauthn
from app.webapp.routers import config as config_router
from app.webapp.routers._helpers import STATIC_DIR
from src.config import load_config
from src.static_versioning import compute_asset_hashes, fleet_hash_of, rewrite_js_imports
from src.webapp_config import load_webapp_config
from src.webauthn_gate import WebAuthnGate

_log = logging.getLogger(__name__)

_LONG_CACHE = "public, max-age=31536000, immutable"
_DAY_CACHE = "public, max-age=86400"
_HASHED_SUFFIXES = {".js", ".css"}
_DAY_CACHE_SUFFIXES = {".webmanifest", ".png", ".ico"}


class _VersionedStatic(StaticFiles):
    """Static mount that stamps Cache-Control + rewrites JS imports.

    JS files get their ``import './foo.js'`` calls rewritten to
    ``import './foo.js?v=<hash>'`` at serve time. Hashed assets get a year-long
    immutable cache; icons and manifest get a day.
    """

    def __init__(self, *, directory: str, asset_hashes: dict[str, str]) -> None:
        super().__init__(directory=directory)
        self._asset_hashes = asset_hashes

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        path = Path(full_path)
        suffix = path.suffix.lower()

        if suffix == ".js":
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                return super().file_response(full_path, stat_result, scope, status_code)
            rewritten = rewrite_js_imports(body, self._asset_hashes)
            media_type, _ = mimetypes.guess_type(str(path))
            return Response(
                content=rewritten,
                status_code=status_code,
                media_type=media_type or "text/javascript",
                headers={"Cache-Control": _LONG_CACHE},
            )

        response = super().file_response(full_path, stat_result, scope, status_code)
        if suffix in _HASHED_SUFFIXES:
            response.headers["Cache-Control"] = _LONG_CACHE
        elif suffix in _DAY_CACHE_SUFFIXES:
            response.headers["Cache-Control"] = _DAY_CACHE
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


def create_app() -> FastAPI:
    webapp_cfg = load_webapp_config()

    auth.ensure_log_handler()

    app = FastAPI(title="WhatsApp Radar", version="0.1.0", lifespan=_lifespan)

    app.add_middleware(
        BearerTokenMiddleware,
        get_token=lambda: getattr(app.state.webapp_config, "auth_token", ""),
    )

    app.state.webapp_config = webapp_cfg
    app.state.webauthn_gate = WebAuthnGate()
    # Resolved once; the dashboard router reads it (tests override app.state.db_path).
    _cfg = load_config()
    app.state.db_path = _cfg.db_path
    # The sidecar router reads the buffer dir from here (tests override it).
    app.state.linked_device_dir = _cfg.linked_device_dir

    asset_hashes = compute_asset_hashes(STATIC_DIR)
    app.state.asset_hashes = asset_hashes
    app.state.asset_fleet_hash = fleet_hash_of(asset_hashes)
    if asset_hashes:
        _log.info(
            "ℹ️ Static assets stamped at fleet hash %s (%d files)",
            app.state.asset_fleet_hash,
            len(asset_hashes),
        )

    if STATIC_DIR.exists():
        app.mount(
            "/static",
            _VersionedStatic(directory=str(STATIC_DIR), asset_hashes=asset_hashes),
            name="static",
        )

    app.include_router(misc.router)
    app.include_router(auth.router)
    app.include_router(webauthn.router)
    app.include_router(dashboard.router)
    app.include_router(chats.router)
    app.include_router(config_router.router)
    app.include_router(execution.router)
    app.include_router(audit.router)
    app.include_router(sidecar.router)

    return app


# Module-level app for `uvicorn app.webapp.server:app`.
app = create_app()
