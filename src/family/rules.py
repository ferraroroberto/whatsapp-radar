"""Deterministic decision logic for the family checks (issue #160).

Pure functions over :class:`calendar_readonly.core.CalendarEvent` and the typed
config — no I/O, no network, no LLM. This is the evidence-backed core: the
OpenClaw postmortem traced its failures to LLM-narrated, non-deterministic
decisions, so classification, origin resolution, dedup, quiet hours, and
conflict detection all live here as plain, unit-tested code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from calendar_readonly.core import CalendarEvent

from src.config import FamilyConfig

_EN_CASA = "(en casa)"
_VIDEO_HINTS = ("meet.google.com", "zoom.us", "teams.microsoft.com", "teams.live.com")


# --------------------------------------------------------------- addresses


def _norm_address(value: str) -> str:
    # Drop punctuation ("Av." / commas) so "Av. Diagonal 621" matches
    # "av diagonal 621, barcelona"; collapse whitespace.
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in value.lower())
    return " ".join(cleaned.split())


def same_address(a: str, b: str) -> bool:
    """True when two address strings refer to the same place (loose match)."""
    a2, b2 = _norm_address(a), _norm_address(b)
    if not a2 or not b2:
        return False
    return a2 == b2 or a2 in b2 or b2 in a2


def _is_video_only(location: str) -> bool:
    """A location that is only a conferencing URL is not a physical place."""
    loc = location.strip()
    if not any(hint in loc.lower() for hint in _VIDEO_HINTS):
        return False
    # Substantial non-URL text alongside the link ⇒ a hybrid physical location.
    return len(loc.split()) <= 1


def physical_location(event: CalendarEvent) -> str | None:
    """The event's physical destination address, or None if it has none."""
    loc = event.location.strip()
    if not loc or _is_video_only(loc):
        return None
    return loc


# --------------------------------------------------------------- classification


def is_en_casa(event: CalendarEvent) -> bool:
    return _EN_CASA in event.summary.lower()


def requires_commute(event: CalendarEvent, home_address: str) -> bool:
    """True when attending this event means physically driving somewhere.

    A physical street address means a commute — even inside town — unless it is
    the home address or the title contains ``(en casa)``. A video link alone
    does not; a hybrid (video link AND a physical address) does.
    """
    if is_en_casa(event):
        return False
    dest = physical_location(event)
    if dest is None:
        return False
    return not same_address(dest, home_address)


def location_kind(event: CalendarEvent, home_address: str) -> str:
    """Daily-scan location class: ``'home'`` | ``'away'`` | ``'unknown'``.

    At-home ONLY if ``(en casa)`` or a video link with no physical address. A
    physical address is 'away' unless it is the home address. **No location at
    all is Unknown, never home** (an explicit production correction).
    """
    if is_en_casa(event):
        return "home"
    dest = physical_location(event)
    if dest is not None:
        return "home" if same_address(dest, home_address) else "away"
    if event.video_link:
        return "home"
    return "unknown"


# --------------------------------------------------------------- origin / commutes


def resolve_origin(
    event: CalendarEvent,
    same_person_events: list[CalendarEvent],
    *,
    home_address: str,
    lookback_min: int,
) -> str:
    """Origin address for a commute to ``event``.

    Look for the same person's other commute events active now or ended within
    ``lookback_min`` before this event's start; use the latest such event's
    destination as the origin (office → lunch chains). Otherwise home.
    """
    window_start = event.start - timedelta(minutes=lookback_min)
    candidates = [
        other
        for other in same_person_events
        if other.event_id != event.event_id
        and other.start < event.start
        and other.end >= window_start
        and requires_commute(other, home_address)
    ]
    if not candidates:
        return home_address
    latest = max(candidates, key=lambda e: e.end)
    return physical_location(latest) or home_address


@dataclass(frozen=True)
class CommuteLeg:
    """One resolved commute to check for traffic."""

    person: str
    event: CalendarEvent
    origin: str
    destination: str


def upcoming_commutes(
    events_by_person: dict[str, list[CalendarEvent]],
    *,
    home_address: str,
    now: datetime,
    lookahead: timedelta,
    origin_lookback_min: int,
) -> list[CommuteLeg]:
    """Every person's imminent commute events within the lookahead window."""
    horizon = now + lookahead
    legs: list[CommuteLeg] = []
    for person, events in events_by_person.items():
        for event in events:
            if event.all_day or not (now <= event.start <= horizon):
                continue
            if not requires_commute(event, home_address):
                continue
            dest = physical_location(event)
            if dest is None:
                continue
            origin = resolve_origin(
                event, events, home_address=home_address, lookback_min=origin_lookback_min
            )
            if same_address(origin, dest):
                continue
            legs.append(CommuteLeg(person=person, event=event, origin=origin, destination=dest))
    return legs


def dedup_key(person: str, event_summary: str) -> str:
    """Stable dedup key for a person + event — the field that always exists."""
    return f"{person}::{' '.join(event_summary.lower().split())}"


# --------------------------------------------------------------- quiet hours


def in_quiet_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    """True if ``now`` falls in the overnight quiet window ``[start, end)``."""
    if start_hour == end_hour:
        return False
    hour = now.hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour  # wraps midnight (e.g. 20..5)


# --------------------------------------------------------------- daily conflicts


@dataclass(frozen=True)
class Conflict:
    """One flagged schedule problem for a given day."""

    kind: str  # 'coverage_gap' | 'unknown_location'
    day: str  # ISO date
    detail: str


def _parse_hhmm(value: str) -> time | None:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _person_away_between(
    events: list[CalendarEvent], start: datetime, end: datetime, home_address: str
) -> bool:
    """True if the person has an 'away' timed event overlapping ``[start, end]``.

    A point-in-time deadline (no configured end) is the degenerate case
    ``start == end`` — identical to the original single-moment containment
    check this generalizes (#167).
    """
    return any(
        not event.all_day
        and location_kind(event, home_address) == "away"
        and event.start <= end
        and event.end >= start
        for event in events
    )


def find_conflicts(
    events_by_person: dict[str, list[CalendarEvent]],
    family: FamilyConfig,
    *,
    day: date,
    tz: tzinfo,
) -> list[Conflict]:
    """Coverage gaps for the day's childcare moments against the fixed pattern.

    For each required moment or window (explicit childcare windows, optionally a
    start-end range, plus the daily kids-home pickup on weekdays), flag when the
    responsible parent has an away commitment overlapping it, or when neither
    parent is available at all. ``tz`` is the household's local zone, used to
    anchor each wall-clock moment against the timezone-aware calendar events.
    """
    weekday = day.weekday()
    windows: list[tuple[str, time, time]] = []
    for window in family.childcare_windows:
        if weekday in window.weekdays:
            start = _parse_hhmm(window.time)
            if start is None:
                continue
            end = _parse_hhmm(window.end_time) if window.end_time else start
            # Config validation (webapp) rejects an inverted end before it is
            # persisted; a malformed legacy config just falls back to the point.
            if end is None or end < start:
                end = start
            windows.append((window.label, start, end))
    kids_home = _parse_hhmm(family.kids_home_time)
    if kids_home is not None and weekday < 5:  # school weekdays
        windows.append(("kids home", kids_home, kids_home))

    responsible = family.responsible_by_weekday.get(weekday)
    conflicts: list[Conflict] = []
    for label, start, end in windows:
        start_dt = datetime.combine(day, start, tzinfo=tz)
        end_dt = datetime.combine(day, end, tzinfo=tz)
        away = {
            person
            for person, events in events_by_person.items()
            if _person_away_between(events, start_dt, end_dt, family.home_address)
        }
        available = [person for person in events_by_person if person not in away]
        if responsible and responsible in away:
            conflicts.append(
                Conflict(
                    kind="coverage_gap",
                    day=day.isoformat(),
                    detail=(
                        f"{responsible} is on duty for '{label}' at "
                        f"{start.strftime('%H:%M')} but has an away commitment"
                    ),
                )
            )
        elif not available:
            conflicts.append(
                Conflict(
                    kind="coverage_gap",
                    day=day.isoformat(),
                    detail=f"No parent is available for '{label}' at {start.strftime('%H:%M')}",
                )
            )
    return conflicts


def find_unknown_locations(
    events_by_person: dict[str, list[CalendarEvent]],
    *,
    home_address: str,
) -> list[tuple[str, CalendarEvent]]:
    """Timed events whose location is Unknown — to ask about, never guess."""
    out: list[tuple[str, CalendarEvent]] = []
    for person, events in events_by_person.items():
        for event in events:
            if not event.all_day and location_kind(event, home_address) == "unknown":
                out.append((person, event))
    return out
