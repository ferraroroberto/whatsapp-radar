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
from dataclasses import dataclass, field
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
class TranscriptionConfig:
    """Voice-note transcription via the local-llm-hub audio endpoint (#36).

    Off by default so the suite stays fully offline and the feature is opt-in (like
    the hub classifier). When enabled, the scan's transcription phase POSTs each
    downloaded voice note to ``{audio_base_url}/v1/audio/transcriptions`` (the hub's
    OpenAI-shape Whisper proxy), transcribe-only.
    """

    # Master switch. When false the transcription phase is a no-op.
    enabled: bool = False
    # Only voice notes from the last N days are transcribed; older ones are marked
    # 'skipped_old' so a fresh pairing never chews through a long backlog.
    window_days: int = 7
    # The hub's audio base URL (its :8000 proxy keeps the call in the hub's
    # observability ring); ``/v1/audio/transcriptions`` is appended by the client.
    audio_base_url: str = "http://127.0.0.1:8000"
    # OpenAI-shape model id sent in the multipart form. ``"whisper-vanilla"`` is the
    # hub's glossary-free turbo path (local-llm-hub#128): it carries no initial
    # prompt and injects ``language=auto`` server-side for requests that omit one, so
    # the source language is auto-detected. The plain turbo (``"whisper-1"`` / no
    # model row) instead carries an English tech-dictation glossary and defaults each
    # languageless request to ``en``, which Englishizes non-English notes into
    # translations — never use it here. See #88.
    model: str = "whisper-vanilla"
    # Whisper language hint. ``"auto"`` (the default) sends none, so whisper-vanilla
    # auto-detects each note's language independently — right for mixed ES/EN content.
    # Pin to an ISO code (e.g. ``"es"``) only if auto-detect proves unreliable.
    language: str = "auto"
    # Per-file transcription request timeout, seconds.
    timeout_seconds: float = 120.0
    # How many days a transcribed voice note's audio is retained on disk so it can
    # be played back in the Chats overlay (#86). After this many days from the
    # note's send time a sweep at the start of each transcription phase deletes the
    # audio and clears its ``media_path``. ``0`` reverts to #36's behaviour: delete
    # the audio immediately on a successful transcription, keep nothing. Audio is
    # more sensitive than text, so this is deliberately short by default and the
    # files never leave the gitignored linked-device buffer dir.
    audio_retention_days: int = 7


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
    # Settled-buffer gate (#73): before a cursor-advancing scan reads the buffer,
    # wait until it has stopped growing for ``sync_settle_seconds`` (history
    # backfill done), hard-capped at ``sync_settle_timeout``. ``0`` disables the
    # gate. Linked-device only; the fixture has no streaming buffer.
    sync_settle_seconds: float = 12.0
    sync_settle_timeout: float = 90.0
    # Voice-note transcription (#36). Defaulted (disabled) so library/test callers
    # that build a Config without it get the offline-safe no-op behaviour.
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)


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
    tr_raw = merged.get("transcription", {})

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
    sync_settle_seconds = float(
        os.environ.get("WR_SYNC_SETTLE_SECONDS", merged.get("sync_settle_seconds", 12.0))
    )
    sync_settle_timeout = float(
        os.environ.get("WR_SYNC_SETTLE_TIMEOUT", merged.get("sync_settle_timeout", 90.0))
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
    transcription = TranscriptionConfig(
        enabled=_as_bool(
            os.environ.get("WR_TRANSCRIPTION_ENABLED"), tr_raw.get("enabled", False)
        ),
        window_days=int(
            os.environ.get("WR_TRANSCRIPTION_WINDOW_DAYS", tr_raw.get("window_days", 7))
        ),
        audio_base_url=os.environ.get(
            "WR_TRANSCRIPTION_AUDIO_BASE_URL",
            tr_raw.get("audio_base_url", "http://127.0.0.1:8000"),
        ),
        model=os.environ.get("WR_TRANSCRIPTION_MODEL", tr_raw.get("model", "whisper-vanilla")),
        language=os.environ.get("WR_TRANSCRIPTION_LANGUAGE", tr_raw.get("language", "auto")),
        timeout_seconds=float(
            os.environ.get("WR_TRANSCRIPTION_TIMEOUT", tr_raw.get("timeout_seconds", 120.0))
        ),
        audio_retention_days=int(
            os.environ.get(
                "WR_TRANSCRIPTION_RETAIN_DAYS", tr_raw.get("audio_retention_days", 7)
            )
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
        transcription=transcription,
        notifier=notifier,
        telegram=telegram,
        linked_device_dir=resolved_buffer,
        sidecar_autostart=sidecar_autostart,
        sync_settle_seconds=sync_settle_seconds,
        sync_settle_timeout=sync_settle_timeout,
    )
