"""Configuration loading.

Precedence (low -> high): ``config/default.json`` (committed) -> ``config/local.json``
(ignored, per-host) -> ``WR_*`` environment variables. ``.env`` is read if present so
the host can set values without exporting them globally; it is never committed.

No secrets live in the committed defaults. Anything host-specific belongs in the
ignored ``config/local.json`` or ``.env``.

This package groups roughly ten independent subsystems (Hub, Transcription, TTS,
Telegram, Tripwire, Gmail, Calendar, Traffic, Presence, Family/Children), each in
its own ``src/config/<domain>.py`` module with its dataclass(es) and a ``parse()``
function. This ``__init__`` owns only what is genuinely cross-cutting: the JSON/env
plumbing (delegated to ``_shared``), the top-level :class:`Config` aggregate, and
``load_config`` wiring the pieces together. Every name below is re-exported so
existing ``from src.config import X`` call sites are unaffected by the split.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from src.config import calendar as _calendar
from src.config import family as _family
from src.config import gmail as _gmail
from src.config import hub as _hub
from src.config import presence as _presence
from src.config import task_os as _task_os
from src.config import telegram as _telegram
from src.config import traffic as _traffic
from src.config import transcription as _transcription
from src.config import tripwire as _tripwire
from src.config import tts as _tts
from src.config._shared import (
    _as_bool,
    _as_sources,
    _deep_merge,
    _load_dotenv,
    _load_json,
    _local_config_path,
    project_root,
    save_local_overrides,
)
from src.config.calendar import CalendarAccount, CalendarConfig
from src.config.family import ChildcareWindow, ChildProfile, FamilyConfig, TravelBlocksConfig
from src.config.gmail import GmailConfig, GmailLabel, GmailSender
from src.config.hub import HubConfig
from src.config.presence import PresenceConfig
from src.config.task_os import TaskOsConfig
from src.config.telegram import TelegramConfig
from src.config.traffic import TrafficConfig
from src.config.transcription import TranscriptionConfig
from src.config.tripwire import TripwireConfig
from src.config.tts import TtsConfig, VoiceProfile

__all__ = [
    "CalendarAccount",
    "CalendarConfig",
    "ChildProfile",
    "ChildcareWindow",
    "Config",
    "FamilyConfig",
    "GmailConfig",
    "GmailLabel",
    "GmailSender",
    "HubConfig",
    "PresenceConfig",
    "TaskOsConfig",
    "TelegramConfig",
    "TrafficConfig",
    "TranscriptionConfig",
    "TravelBlocksConfig",
    "TripwireConfig",
    "TtsConfig",
    "VoiceProfile",
    "load_config",
    "project_root",
    "save_local_overrides",
]


@dataclass(frozen=True)
class Config:
    db_path: Path
    connector: str
    classifier: str
    hub: HubConfig
    notifier: str
    telegram: TelegramConfig
    linked_device_dir: Path
    tripwire: TripwireConfig = field(default_factory=TripwireConfig)
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
    # Live phone-location lookup (#169). Defaulted (disabled) so library/test
    # callers that build a Config without it get the offline-safe no-op behaviour.
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    # task-os Inbox export (#307). Defaulted (disabled) so library/test callers
    # that build a Config without it get the offline-safe no-op behaviour.
    task_os: TaskOsConfig = field(default_factory=TaskOsConfig)
    # Household child registry (#206/#215). Empty by default — ``default.json``
    # ships an empty placeholder; real entries only ever live in the gitignored
    # ``config/local.json``. Used to resolve which child a Gmail school-source
    # email concerns (Stage-2 hint injection, HubClassifier._build_user_prompt).
    children: tuple[ChildProfile, ...] = ()


def load_config(root: Path | None = None) -> Config:
    """Build the effective :class:`Config` from defaults, local overrides, and env."""
    root = root or project_root()
    _load_dotenv(root / ".env")

    merged = _deep_merge(
        _load_json(root / "config" / "default.json"),
        _load_json(_local_config_path(root)),
    )
    hub_raw = merged.get("hub", {})
    tr_raw = merged.get("transcription", {})
    gmail_raw = merged.get("gmail", {})
    tripwire_raw = merged.get("tripwire", {})
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

    hub = _hub.parse(hub_raw)
    transcription = _transcription.parse(tr_raw)
    tts = _tts.parse(tts_raw)
    telegram = _telegram.parse(tg_raw)
    tripwire = _tripwire.parse(tripwire_raw)
    gmail = _gmail.parse(gmail_raw, root)
    calendar = _calendar.parse(merged.get("calendar", {}), root)
    traffic = _traffic.parse(merged.get("traffic", {}))
    family = _family.parse(merged.get("family", {}))
    presence = _presence.parse(merged.get("presence", {}))
    task_os = _task_os.parse(merged.get("task_os", {}))
    children = _family.parse_children(merged.get("children", []))

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
        tts=tts,
        notifier=notifier,
        telegram=telegram,
        linked_device_dir=resolved_buffer,
        tripwire=tripwire,
        sidecar_autostart=sidecar_autostart,
        sync_settle_seconds=sync_settle_seconds,
        sync_settle_timeout=sync_settle_timeout,
        sources=sources,
        gmail=gmail,
        calendar=calendar,
        traffic=traffic,
        family=family,
        presence=presence,
        task_os=task_os,
        children=children,
    )
