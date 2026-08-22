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
class TravelBlocksConfig:
    """Auto-written commute travel blocks (#265, umbrella #263). Off by default.

    ``dry_run`` defaults **on** even once ``enabled`` is flipped: the first live
    days should log the plan and touch nothing, because this feature writes to
    (and later deletes from) real household calendars. ``horizon_days`` mirrors
    ``FamilyConfig.assessment_days`` so the sweep maintains exactly the window
    the daily scan already reasons about.

    ``min_home_dwell_min`` is the chaining knob: below
    ``drive_home + drive_out + min_home_dwell_min`` of gap between two events
    there is not enough time at home for the round trip to be worth anything,
    so a direct A→B hop is assumed and no return-home block is written. Keep it
    roughly consistent with ``traffic.origin_lookback_min``, which is the
    authority on the *outbound* side (``rules.resolve_origin``): a much larger
    dwell threshold makes the two disagree for mid-length gaps, and the
    conservative outcome — no return block, a fresh outbound from home — is
    what you get.

    ``title_template`` is deliberately not the destination: a shared calendar
    view must leak nothing about where the person is going.
    """

    enabled: bool = False
    dry_run: bool = True
    horizon_days: int = 2
    min_home_dwell_min: int = 45
    title_template: str = "🚗 Trayecto"


@dataclass(frozen=True)
class FamilyConfig:
    """Daily calendar-conflict scan knobs + the fixed household schedule (#160).

    Personal household detail (home address, the who-is-home pattern, childcare
    windows) lives only in the gitignored ``config/local.json``; the committed
    ``default.json`` ships empty placeholders with the scan disabled.
    """

    enabled: bool = False
    # Local hour before which `wr calendar-scan` self-skips (#277). It is a
    # *floor*, not a fire time: the App Launcher job decides when the verb is
    # invoked, this decides whether an unforced invocation does anything. An
    # explicit `--force` (every webapp button) ignores it entirely.
    run_hour: int = 7
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
    # Auto-written commute travel blocks (#265). Defaulted (disabled, dry-run)
    # so library/test callers that build a FamilyConfig without it get the
    # write-nothing behaviour.
    travel_blocks: TravelBlocksConfig = field(default_factory=TravelBlocksConfig)


def parse_travel_blocks(raw: dict[str, Any]) -> TravelBlocksConfig:
    """Parse the ``family.travel_blocks`` sub-block. No ``WR_`` env overrides.

    Deliberately plain ``raw.get``: unlike ``family.enabled`` these knobs are
    never toggled from a scheduler environment — they are edited in
    ``config/local.json`` (or, from step 4 of #263, the webapp Family tab).
    """
    defaults = TravelBlocksConfig()
    return TravelBlocksConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        dry_run=bool(raw.get("dry_run", defaults.dry_run)),
        horizon_days=int(raw.get("horizon_days", defaults.horizon_days)),
        min_home_dwell_min=int(raw.get("min_home_dwell_min", defaults.min_home_dwell_min)),
        title_template=str(raw.get("title_template") or defaults.title_template).strip(),
    )


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
        travel_blocks=parse_travel_blocks(raw.get("travel_blocks") or {}),
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
