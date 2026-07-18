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
    # 'skipped_old' so a fresh pairing never chews through a long backlog. This gates
    # only *never-attempted* ('pending') notes — see ``failed_retry_days`` for notes
    # we already tried and that failed.
    window_days: int = 7
    # How long a note that already *failed* transcription keeps being retried (and its
    # audio kept on disk) before we give up, mark it 'skipped_old' and delete the audio.
    # A failed note means a transient outage (e.g. the whisper backend was down, #99 /
    # local-llm-hub#147), not first-pairing backlog, so it retries on every full sync
    # regardless of ``window_days`` — but bounded here so sensitive audio isn't kept
    # forever. Deliberately longer than ``window_days`` so a multi-day outage always
    # recovers; never below it in practice (#104).
    failed_retry_days: int = 30
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
class VoiceProfile:
    """A hub model + voice pair behind one logical summary-speech profile."""

    model: str
    voice: str


@dataclass(frozen=True)
class TtsConfig:
    """Model/voice pairs behind the four summary read-aloud profiles (#157).

    Keyed by ``"{language}_{gender}"``; :func:`src.speech_profile.resolve_profile_key`
    picks which one applies to a given message. English keeps the existing
    expressive ``orpheus-tts`` voices App Launcher established; Spanish uses the
    hub's ``kokoro-tts`` model, whose bundled voice pack ships a stable
    Spanish-capable female/male pair (``ef_dora`` / ``em_alex``) — no second TTS
    runtime and no prerequisite local-llm-hub change needed.
    """

    en_female: VoiceProfile = field(default_factory=lambda: VoiceProfile("orpheus-tts", "tara"))
    en_male: VoiceProfile = field(default_factory=lambda: VoiceProfile("orpheus-tts", "leo"))
    es_female: VoiceProfile = field(
        default_factory=lambda: VoiceProfile("kokoro-tts", "ef_dora")
    )
    es_male: VoiceProfile = field(default_factory=lambda: VoiceProfile("kokoro-tts", "em_alex"))

    def get(self, profile_key: str) -> VoiceProfile:
        """The :class:`VoiceProfile` for a ``"{language}_{gender}"`` key."""
        profiles: dict[str, VoiceProfile] = {
            "en_female": self.en_female,
            "en_male": self.en_male,
            "es_female": self.es_female,
            "es_male": self.es_male,
        }
        return profiles[profile_key]


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass(frozen=True)
class GmailSender:
    """One explicitly allowed sender represented as a stable Gmail chat."""

    address: str
    name: str


@dataclass(frozen=True)
class GmailLabel:
    """One explicitly allowed Gmail label represented as a stable chat."""

    name: str
    display_name: str


@dataclass(frozen=True)
class GmailConfig:
    """Read-only Gmail API credentials and whitelist."""

    credentials_path: Path = Path("auth/gmail/credentials.json")
    token_path: Path = Path("auth/gmail/token.json")
    senders: tuple[GmailSender, ...] = ()
    labels: tuple[GmailLabel, ...] = ()


@dataclass(frozen=True)
class CalendarAccount:
    """One household calendar and the person it belongs to."""

    calendar_id: str  # the calendar id (an email address)
    person: str  # canonical person key, e.g. "roberto" / "ana"
    label: str = ""  # optional display label


@dataclass(frozen=True)
class CalendarConfig:
    """Read-only Google Calendar credentials + the household calendars (#160)."""

    credentials_path: Path = Path("auth/calendar/credentials.json")
    token_path: Path = Path("auth/calendar/token.json")
    accounts: tuple[CalendarAccount, ...] = ()


@dataclass(frozen=True)
class TrafficConfig:
    """Traffic-jam check knobs (Google Routes API v2). Disabled by default (#160).

    ``api_key`` is a secret and lives only in the ignored ``config/local.json``
    (or ``WR_TRAFFIC_API_KEY`` / ``GOOGLE_MAPS_API_KEY``), never the committed
    defaults. Quiet hours pause checks overnight; only a delay over
    ``significant_delay_min`` alerts, deduped within ``dedup_window_min``.
    """

    enabled: bool = False
    api_key: str = ""
    significant_delay_min: int = 15
    quiet_start_hour: int = 20  # local hour checks pause at (inclusive)
    quiet_end_hour: int = 5  # local hour checks resume at
    dedup_window_min: int = 30
    origin_lookback_min: int = 60
    lookahead_hours: int = 3  # how far ahead to look for the next commute


@dataclass(frozen=True)
class ChildcareWindow:
    """A recurring childcare moment a parent must be present for."""

    label: str
    weekdays: tuple[int, ...]  # 0=Mon .. 6=Sun
    time: str  # "HH:MM" deadline (pickup / departure)


@dataclass(frozen=True)
class FamilyConfig:
    """Daily calendar-conflict scan knobs + the fixed household schedule (#160).

    Personal household detail (home address, the who-is-home pattern, childcare
    windows) lives only in the gitignored ``config/local.json``; the committed
    ``default.json`` ships empty placeholders with the scan disabled.
    """

    enabled: bool = False
    run_hour: int = 7  # local hour the daily scan fires at/after
    home_address: str = ""
    kids_home_time: str = "17:30"
    responsible_by_weekday: dict[int, str] = field(default_factory=dict)  # 0..6 -> person
    childcare_windows: tuple[ChildcareWindow, ...] = ()
    unknown_scan_days: int = 7
    assessment_days: int = 2


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
    # Summary read-aloud voice profiles (#157). Defaulted so library/test callers
    # that build a Config without it still get sane model/voice pairs.
    tts: TtsConfig = field(default_factory=TtsConfig)
    # Enabled logical message sources. ``connector`` remains the WhatsApp reader
    # implementation selector (fixture | linked_device) for backwards
    # compatibility; additional sources own their own connector configuration.
    sources: tuple[str, ...] = ("whatsapp",)
    gmail: GmailConfig = field(default_factory=GmailConfig)
    # Family calendar-conflict + traffic-jam checks (#160). Independent of the
    # message pipeline above; both default disabled until creds are provisioned.
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    traffic: TrafficConfig = field(default_factory=TrafficConfig)
    family: FamilyConfig = field(default_factory=FamilyConfig)


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


def _as_sources(value: str | list[Any] | tuple[Any, ...] | None) -> tuple[str, ...]:
    """Normalize a JSON list or comma-separated ``WR_SOURCES`` value.

    Source order is stable and duplicates are removed. An empty/invalid value
    falls back to WhatsApp so the historical single-source configuration keeps
    working instead of silently disabling ingestion.
    """
    raw = value.split(",") if isinstance(value, str) else (value or [])
    sources: list[str] = []
    for item in raw:
        source = str(item).strip().lower()
        if source and source not in sources:
            sources.append(source)
    return tuple(sources or ["whatsapp"])


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


_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _weekday_index(value: Any) -> int | None:
    """Coerce a weekday (``"mon"``/``"monday"`` or ``0``-``6``) to a 0=Mon index."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 6:
        return value
    key = str(value).strip().lower()[:3]
    return _WEEKDAYS.get(key)


def _parse_calendar(raw: dict[str, Any], root: Path) -> CalendarConfig:
    creds = Path(
        os.environ.get(
            "WR_CALENDAR_CREDENTIALS_PATH",
            raw.get("credentials_path", "auth/calendar/credentials.json"),
        )
    )
    if not creds.is_absolute():
        creds = root / creds
    token = Path(
        os.environ.get(
            "WR_CALENDAR_TOKEN_PATH", raw.get("token_path", "auth/calendar/token.json")
        )
    )
    if not token.is_absolute():
        token = root / token
    accounts = tuple(
        CalendarAccount(
            calendar_id=str(item.get("calendar_id", "")).strip(),
            person=str(item.get("person", "")).strip().lower(),
            label=str(item.get("label") or item.get("person") or "").strip(),
        )
        for item in raw.get("accounts", [])
        if isinstance(item, dict) and str(item.get("calendar_id", "")).strip()
    )
    return CalendarConfig(credentials_path=creds, token_path=token, accounts=accounts)


def _parse_traffic(raw: dict[str, Any]) -> TrafficConfig:
    api_key = (
        os.environ.get("WR_TRAFFIC_API_KEY")
        or os.environ.get("GOOGLE_MAPS_API_KEY")
        or str(raw.get("api_key", ""))
    )
    return TrafficConfig(
        enabled=_as_bool(os.environ.get("WR_TRAFFIC_ENABLED"), raw.get("enabled", False)),
        api_key=api_key,
        significant_delay_min=int(raw.get("significant_delay_min", 15)),
        quiet_start_hour=int(raw.get("quiet_start_hour", 20)),
        quiet_end_hour=int(raw.get("quiet_end_hour", 5)),
        dedup_window_min=int(raw.get("dedup_window_min", 30)),
        origin_lookback_min=int(raw.get("origin_lookback_min", 60)),
        lookahead_hours=int(raw.get("lookahead_hours", 3)),
    )


def _parse_family(raw: dict[str, Any]) -> FamilyConfig:
    responsible: dict[int, str] = {}
    for key, person in (raw.get("responsible_by_weekday") or {}).items():
        idx = _weekday_index(key)
        if idx is not None and str(person).strip():
            responsible[idx] = str(person).strip().lower()
    windows = tuple(
        ChildcareWindow(
            label=str(item.get("label", "")).strip(),
            weekdays=tuple(
                idx
                for idx in (_weekday_index(d) for d in item.get("weekdays", []))
                if idx is not None
            ),
            time=str(item.get("time", "")).strip(),
        )
        for item in raw.get("childcare_windows", [])
        if isinstance(item, dict) and str(item.get("label", "")).strip()
    )
    return FamilyConfig(
        enabled=_as_bool(os.environ.get("WR_FAMILY_ENABLED"), raw.get("enabled", False)),
        run_hour=int(raw.get("run_hour", 7)),
        home_address=str(raw.get("home_address", "")).strip(),
        kids_home_time=str(raw.get("kids_home_time", "17:30")).strip(),
        responsible_by_weekday=responsible,
        childcare_windows=windows,
        unknown_scan_days=int(raw.get("unknown_scan_days", 7)),
        assessment_days=int(raw.get("assessment_days", 2)),
    )


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
    gmail_raw = merged.get("gmail", {})
    tts_raw = (merged.get("tts") or {}).get("profiles", {})

    tg_raw = merged.get("telegram", {})

    db_path = os.environ.get("WR_DB_PATH", merged.get("db_path", "data/whatsapp-radar.sqlite3"))
    connector = os.environ.get("WR_CONNECTOR", merged.get("connector", "fixture"))
    sources = _as_sources(os.environ.get("WR_SOURCES") or merged.get("sources"))
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
        failed_retry_days=int(
            os.environ.get(
                "WR_TRANSCRIPTION_FAILED_RETRY_DAYS", tr_raw.get("failed_retry_days", 30)
            )
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
    def _voice_profile(key: str, default: VoiceProfile) -> VoiceProfile:
        entry = tts_raw.get(key) if isinstance(tts_raw, dict) else None
        if not isinstance(entry, dict):
            return default
        return VoiceProfile(
            model=str(entry.get("model", default.model)),
            voice=str(entry.get("voice", default.voice)),
        )

    _tts_defaults = TtsConfig()
    tts = TtsConfig(
        en_female=_voice_profile("en_female", _tts_defaults.en_female),
        en_male=_voice_profile("en_male", _tts_defaults.en_male),
        es_female=_voice_profile("es_female", _tts_defaults.es_female),
        es_male=_voice_profile("es_male", _tts_defaults.es_male),
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

    gmail_credentials = Path(
        os.environ.get(
            "WR_GMAIL_CREDENTIALS_PATH",
            gmail_raw.get("credentials_path", "auth/gmail/credentials.json"),
        )
    )
    if not gmail_credentials.is_absolute():
        gmail_credentials = root / gmail_credentials
    gmail_token = Path(
        os.environ.get(
            "WR_GMAIL_TOKEN_PATH",
            gmail_raw.get("token_path", "auth/gmail/token.json"),
        )
    )
    if not gmail_token.is_absolute():
        gmail_token = root / gmail_token
    gmail = GmailConfig(
        credentials_path=gmail_credentials,
        token_path=gmail_token,
        senders=tuple(
            GmailSender(
                address=str(item.get("address", "")).strip().lower(),
                name=str(item.get("name") or item.get("address") or "").strip(),
            )
            for item in gmail_raw.get("senders", [])
            if isinstance(item, dict) and str(item.get("address", "")).strip()
        ),
        labels=tuple(
            GmailLabel(
                name=str(item.get("name", "")).strip(),
                display_name=str(
                    item.get("display_name") or item.get("name") or ""
                ).strip(),
            )
            for item in gmail_raw.get("labels", [])
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ),
    )

    calendar = _parse_calendar(merged.get("calendar", {}), root)
    traffic = _parse_traffic(merged.get("traffic", {}))
    family = _parse_family(merged.get("family", {}))

    return Config(
        db_path=resolved_db,
        connector=connector,
        classifier=classifier,
        hub=hub,
        transcription=transcription,
        tts=tts,
        notifier=notifier,
        telegram=telegram,
        linked_device_dir=resolved_buffer,
        sidecar_autostart=sidecar_autostart,
        sync_settle_seconds=sync_settle_seconds,
        sync_settle_timeout=sync_settle_timeout,
        sources=sources,
        gmail=gmail,
        calendar=calendar,
        traffic=traffic,
        family=family,
    )
