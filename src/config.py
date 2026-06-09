"""Configuration loading.

Precedence (low -> high): ``config/default.json`` (committed) -> ``config/local.json``
(ignored, per-host) -> ``WR_*`` environment variables. ``.env`` is read if present so
the host can set values without exporting them globally; it is never committed.

No secrets live in the committed defaults. Anything host-specific belongs in the
ignored ``config/local.json`` or ``.env``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def project_root() -> Path:
    """Repository root (the directory containing ``config/`` and ``pyproject.toml``)."""
    return Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class HubConfig:
    base_url: str
    model: str
    # Output token budget for one classification call. Sized per model rather
    # than hard-coded so a reasoning model with a long <think> trace can be given
    # room instead of silently truncating mid-think.
    max_tokens: int = 8192
    # Max characters of the rendered message delta sent in one prompt. Caps a
    # whole-history scan so a single request can't blow the model's context.
    max_prompt_chars: int = 24000
    # How many days of already-surfaced actionable alerts to feed Stage 2 as
    # short-term memory, so a repeated to-do isn't re-alerted every run (#66).
    recent_alert_days: int = 7


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass(frozen=True)
class Config:
    db_path: Path
    connector: str
    classifier: str
    hub: HubConfig
    notifier: str
    telegram: TelegramConfig
    linked_device_dir: Path
    # When the live source is the linked-device sidecar, a preflight may relaunch
    # it automatically if it has stopped (issue #29). Off skips the self-heal and
    # simply aborts the run loudly when the source is offline.
    sidecar_autostart: bool = True


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: ``KEY=value`` lines into ``os.environ`` if not already set."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def _as_bool(env_value: str | None, default: bool) -> bool:
    """Coerce an env string (or fall back to ``default``) to a bool.

    Accepts the usual truthy/falsy spellings; an unrecognized value keeps the
    default rather than silently flipping the flag.
    """
    if env_value is None:
        return bool(default)
    token = env_value.strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def save_local_overrides(partial: dict[str, Any], root: Path | None = None) -> Path:
    """Deep-merge ``partial`` into the gitignored ``config/local.json`` (atomic).

    This is the per-host override layer the webapp's safe-settings form writes to
    — never the committed ``config/default.json``. Existing keys not present in
    ``partial`` are preserved. Returns the path written.
    """
    root = root or project_root()
    target = root / "config" / "local.json"
    current = _load_json(target)
    merged = _deep_merge(current, partial)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def load_config(root: Path | None = None) -> Config:
    """Build the effective :class:`Config` from defaults, local overrides, and env."""
    root = root or project_root()
    _load_dotenv(root / ".env")

    merged = _deep_merge(
        _load_json(root / "config" / "default.json"),
        _load_json(root / "config" / "local.json"),
    )
    hub_raw = merged.get("hub", {})

    tg_raw = merged.get("telegram", {})

    db_path = os.environ.get("WR_DB_PATH", merged.get("db_path", "data/whatsapp-radar.sqlite3"))
    connector = os.environ.get("WR_CONNECTOR", merged.get("connector", "fixture"))
    classifier = os.environ.get("WR_CLASSIFIER", merged.get("classifier", "stub"))
    notifier = os.environ.get("WR_NOTIFIER", merged.get("notifier", "none"))
    linked_device_dir = os.environ.get(
        "WR_LINKED_DEVICE_DIR", merged.get("linked_device_dir", "data/linked_device")
    )
    sidecar_autostart = _as_bool(
        os.environ.get("WR_SIDECAR_AUTOSTART"), merged.get("sidecar_autostart", True)
    )
    hub = HubConfig(
        base_url=os.environ.get(
            "WR_HUB_BASE_URL", hub_raw.get("base_url", "http://127.0.0.1:8000")
        ),
        model=os.environ.get("WR_HUB_MODEL", hub_raw.get("model", "claude_sonnet")),
        max_tokens=int(os.environ.get("WR_HUB_MAX_TOKENS", hub_raw.get("max_tokens", 8192))),
        max_prompt_chars=int(
            os.environ.get("WR_HUB_MAX_PROMPT_CHARS", hub_raw.get("max_prompt_chars", 24000))
        ),
        recent_alert_days=int(
            os.environ.get("WR_HUB_RECENT_ALERT_DAYS", hub_raw.get("recent_alert_days", 7))
        ),
    )
    # Telegram secrets live in the gitignored config/webapp_config.json (Step 3)
    # so the webapp UI owns them. Precedence: WR_TELEGRAM_* env > webapp_config >
    # local.json/default.json. Imported lazily to avoid a config import cycle.
    from src.webapp_config import load_webapp_config

    wcfg = load_webapp_config()
    tg_bot_default = wcfg.telegram_bot_token or tg_raw.get("bot_token", "")
    tg_chat_default = wcfg.telegram_chat_id or tg_raw.get("chat_id", "")
    telegram = TelegramConfig(
        bot_token=os.environ.get("WR_TELEGRAM_BOT_TOKEN", tg_bot_default),
        chat_id=os.environ.get("WR_TELEGRAM_CHAT_ID", tg_chat_default),
    )

    resolved_db = Path(db_path)
    if not resolved_db.is_absolute():
        resolved_db = root / resolved_db

    resolved_buffer = Path(linked_device_dir)
    if not resolved_buffer.is_absolute():
        resolved_buffer = root / resolved_buffer

    return Config(
        db_path=resolved_db,
        connector=connector,
        classifier=classifier,
        hub=hub,
        notifier=notifier,
        telegram=telegram,
        linked_device_dir=resolved_buffer,
        sidecar_autostart=sidecar_autostart,
    )
