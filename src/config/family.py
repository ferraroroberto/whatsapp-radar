"""Daily calendar-conflict scan knobs + the household child registry (#160/#206/#215)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from src.config._shared import _as_bool

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _weekday_index(value: Any) -> int | None:
    """Coerce a weekday (``"mon"``/``"monday"`` or ``0``-``6``) to a 0=Mon index."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 6:
        return value
    key = str(value).strip().lower()[:3]
    return _WEEKDAYS.get(key)


@dataclass(frozen=True)
class ChildcareWindow:
    """A recurring childcare moment (or time range) a parent must be present for."""

    label: str
    weekdays: tuple[int, ...]  # 0=Mon .. 6=Sun
    time: str  # "HH:MM" start / deadline (pickup / departure)
    end_time: str = ""  # "HH:MM" end; blank = a point-in-time deadline (#167)


@dataclass(frozen=True)
class ChildProfile:
    """One registered household child a Gmail school email might be about (#206/#215).

    Personal, household-identifying detail — real entries live only in the
    gitignored ``config/local.json``, mirroring ``GmailConfig.senders``/``.labels``.
    ``aliases`` are extra name-matching hints (nicknames, how the school addresses
    the child) beyond ``name`` itself; ``class_name`` is used verbatim in the
    classifier hint since school naming conventions vary.
    """

    name: str
    aliases: tuple[str, ...] = ()
    class_name: str = ""


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
    # Whether the daily summary asks for locations on events that have none
    # (#253). Off means the "📍 No location set" section is left out of the
    # Telegram message — nothing else changes: the events are still assumed
    # home, still flagged `assumed` in the decision trace, and still listed in
    # the run's `missing_locations`. The setting silences the nag, it does not
    # make the assumption invisible (the whole point of #168).
    ask_missing_locations: bool = True
    # Routine-prep calendar reminders (#218, Step 4/5 of #206). An empty
    # `reminder_calendar_id` means the feature is off: the pipeline never builds
    # a calendar_write client and no event is ever created. `reminder_time` is
    # the local "HH:MM" morning slot each reminder event is created at.
    reminder_calendar_id: str = ""
    reminder_time: str = "07:30"


def parse(raw: dict[str, Any]) -> FamilyConfig:
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
            end_time=str(item.get("end_time", "")).strip(),
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
        ask_missing_locations=_as_bool(
            os.environ.get("WR_FAMILY_ASK_MISSING_LOCATIONS"),
            raw.get("ask_missing_locations", True),
        ),
        reminder_calendar_id=str(raw.get("reminder_calendar_id", "")).strip(),
        reminder_time=str(raw.get("reminder_time", "07:30")).strip(),
    )


def parse_children(raw: list[Any]) -> tuple[ChildProfile, ...]:
    return tuple(
        ChildProfile(
            name=str(item.get("name", "")).strip(),
            aliases=tuple(str(a).strip() for a in item.get("aliases", []) if str(a).strip()),
            class_name=str(item.get("class_name", "")).strip(),
        )
        for item in raw
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    )
