"""Webapp-authored configuration loader.

Holds the settings the admin web app owns and persists across runs — network
knobs, auth secrets, the WebAuthn relying-party identity, and (migrated off
``.env`` in Step 3) the Telegram delivery secrets. Kept separate from
``config/default.json`` (committed, non-secret) because this file is written
from the UI and is gitignored.

``src/config.py`` also reads the Telegram fields here so the CLI and webapp
share one source of truth.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "webapp_config.json"
SAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "webapp_config.sample.json"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8455


@dataclass
class WebappConfig:
    """User-authored, persisted webapp settings."""

    enabled: bool = True
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # Bearer token enforced when the request did NOT come from a loopback IP.
    # Empty string disables enforcement entirely.
    auth_token: str = ""
    # Optional password gate that hands the bearer token back to the browser
    # when typed correctly. Lets a fresh device bootstrap without a tokenised URL.
    auth_password: str = ""
    # Extra IPs / CIDRs allowed to reach passkey endpoints on top of loopback +
    # the Tailscale CGNAT range (100.64.0.0/10). Empty by default.
    tailnet_allowlist: list[str] = field(default_factory=list)
    # WebAuthn relying-party identity. rp_id is the bare tailnet hostname
    # (e.g. "pc.tailnet.ts.net"); origin is the full https origin the phone
    # connects to. Empty disables the passkey gate.
    webauthn_rp_id: str = ""
    webauthn_rp_name: str = "WhatsApp Radar"
    webauthn_origin: str = ""
    # Telegram delivery secrets, migrated here from .env in Step 3.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def load_webapp_config(path: Path | None = None) -> WebappConfig:
    """Load the webapp config, falling back to defaults if the file is missing."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not target.exists():
        logger.info(
            f"📂 webapp_config not found at {target}, using defaults "
            f"(file is created when settings change)"
        )
        return WebappConfig()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"⚠️  Could not read {target} ({exc}); falling back to defaults")
        return WebappConfig()

    cfg = WebappConfig(
        enabled=bool(raw.get("enabled", True)),
        host=str(raw.get("host", DEFAULT_HOST)),
        port=int(raw.get("port", DEFAULT_PORT)),
        auth_token=str(raw.get("auth_token", "")),
        auth_password=str(raw.get("auth_password", "")),
        tailnet_allowlist=[str(p) for p in (raw.get("tailnet_allowlist") or [])],
        webauthn_rp_id=str(raw.get("webauthn_rp_id", "")),
        webauthn_rp_name=str(raw.get("webauthn_rp_name", "WhatsApp Radar")),
        webauthn_origin=str(raw.get("webauthn_origin", "")),
        telegram_bot_token=str(raw.get("telegram_bot_token", "")),
        telegram_chat_id=str(raw.get("telegram_chat_id", "")),
    )
    _validate(cfg)
    return cfg


def save_webapp_config(cfg: WebappConfig, path: Path | None = None) -> Path:
    """Atomically write the config back to disk."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "enabled": cfg.enabled,
        "host": cfg.host,
        "port": cfg.port,
        "auth_token": cfg.auth_token,
        "auth_password": cfg.auth_password,
        "tailnet_allowlist": cfg.tailnet_allowlist,
        "webauthn_rp_id": cfg.webauthn_rp_id,
        "webauthn_rp_name": cfg.webauthn_rp_name,
        "webauthn_origin": cfg.webauthn_origin,
        "telegram_bot_token": cfg.telegram_bot_token,
        "telegram_chat_id": cfg.telegram_chat_id,
    }

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    logger.info(f"💾 Saved webapp_config to {target}")
    return target


def update_webapp_config(**fields: Any) -> WebappConfig:
    """Read, patch, save — convenience for the API endpoint."""
    current = load_webapp_config()
    patched = replace(current, **fields)
    _validate(patched)
    save_webapp_config(patched)
    return patched


def append_auth_token(url: str, token: str | None) -> str:
    """Return ``url`` with ``?token=<token>`` appended when ``token`` is set."""
    if not token:
        return url
    parsed = urlparse(url)
    existing = parsed.query
    extra = urlencode({"token": token})
    new_query = f"{existing}&{extra}" if existing else extra
    return urlunparse(parsed._replace(query=new_query))


def _validate(cfg: WebappConfig) -> None:
    if not (1 <= cfg.port <= 65535):
        raise ValueError(f"port out of range: {cfg.port}")
