"""Password login → bearer token swap.

The bearer token in ``webapp_config.json`` is never sent to the client until a
correct password is presented. Failed attempts and successful logins both land
in ``webapp/auth.log`` (separate from the main log so a phone-side review is easy).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.webapp.middleware import is_pc_itself
from app.webapp.routers._helpers import maybe_json
from src import password
from src.config import load_config
from src.db.delivery_status import DELIVERY_FAILED
from src.delivery.alert import send_alert
from src.login_lockout import LockoutAlert, LoginLockout, retry_after_header
from src.paths import PROJECT_ROOT
from src.runtime_files import file_handler, make_dirs
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)
auth_logger = logging.getLogger("whatsapp_radar.auth")
_AUTH_LOG_PATH = PROJECT_ROOT / "webapp" / "auth.log"


def ensure_log_handler() -> None:
    """Attach the auth.log file handler exactly once. Idempotent — safe to call
    from ``create_app()`` on every boot."""
    if any(
        isinstance(h, logging.FileHandler)
        and Path(h.baseFilename).resolve() == _AUTH_LOG_PATH.resolve()
        for h in auth_logger.handlers
    ):
        return
    try:
        make_dirs(_AUTH_LOG_PATH.parent)
        fh = file_handler(_AUTH_LOG_PATH)
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        auth_logger.addHandler(fh)
        auth_logger.setLevel(logging.INFO)
    except OSError as exc:
        logger.warning(f"⚠️  Could not open {_AUTH_LOG_PATH}: {exc}")


_APP_NAME = "Family Radar"


def _send_login_alert(alert: LockoutAlert) -> None:
    """Tell the owner someone is guessing the password, off the request thread."""
    held = f" (+{alert.suppressed} more held back)" if alert.suppressed else ""
    text = f"🚨 {_APP_NAME}: {alert.failures} wrong passwords in a row from {alert.key}{held}"

    def send() -> None:
        # Config is read here, not at boot, so the current chat id and token apply.
        status, detail = send_alert(load_config(), text)
        if status == DELIVERY_FAILED:
            logger.warning(f"⚠️  login alert not delivered: {detail}")

    threading.Thread(target=send, name="login-alert", daemon=True).start()


def build_login_lockout(clock: Callable[[], float] = time.monotonic) -> LoginLockout:
    """The one per-app lock-out; ``create_app`` stores it on ``app.state``."""
    return LoginLockout(on_alert=_send_login_alert, clock=clock)


router = APIRouter()


@router.post("/api/login")
async def login(request: Request) -> dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    client_host = request.client.host if request.client else "?"
    lockout: LoginLockout = request.app.state.login_lockout
    # The PC itself never locks out: the owner at the keyboard is always let in.
    exempt = is_pc_itself(request)
    wait = 0.0 if exempt else lockout.retry_after(client_host)
    if wait > 0:
        auth_logger.warning(f"🔒 Login refused for {client_host}: locked out for {wait:.0f}s")
        raise HTTPException(
            status_code=429, detail="too many attempts", headers=retry_after_header(wait)
        )
    if not password.is_configured(legacy=cfg.auth_password):
        auth_logger.info(
            f"⚠️  Login attempt from {client_host} but no password configured"
        )
        raise HTTPException(status_code=503, detail="password auth not configured")
    if not cfg.auth_token:
        auth_logger.info(
            f"⚠️  Login attempt from {client_host} but no auth_token configured"
        )
        raise HTTPException(status_code=503, detail="bearer token not configured")
    body = await maybe_json(request)
    presented = str(body.get("password") or "")
    if not password.check_password(presented, legacy=cfg.auth_password):
        auth_logger.warning(
            f"🚨 Failed password attempt from {client_host} "
            f"(presented: {len(presented)} chars)"
        )
        if not exempt:
            lockout.record_failure(client_host)
        raise HTTPException(status_code=401, detail="bad password")
    lockout.reset(client_host)
    auth_logger.info(f"🔓 Password login from {client_host}")
    return {"token": cfg.auth_token}
