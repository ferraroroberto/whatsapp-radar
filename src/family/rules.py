"""Deterministic decision logic for the family checks (issue #160).

Pure functions over :class:`calendar_readonly.core.CalendarEvent` and the typed
config — no I/O, no network, no LLM. This is the evidence-backed core: the
OpenClaw postmortem traced its failures to LLM-narrated, non-deterministic
decisions, so classification, origin resolution, dedup, quiet hours, and
conflict detection all live here as plain, unit-tested code.
"""

from __future__ import annotations

import math
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


@dataclass(frozen=True)
class LocationDecision:
    """Where an event places its person, and *why* — the traceable unit (#168).

    ``assumed`` marks the missing-location fallback: no address, no video link,
    no ``(en casa)`` marker ⇒ treated as home for the conflict math, but flagged
    so the daily summary can ask for the location to be filled in. This
    replaces the earlier Unknown-never-home semantics: the assumption is now
    explicit and visible instead of a silent third state.
    """

    kind: str  # 'home' | 'away'
    source: str  # 'en_casa_marker' | 'home_address' | 'physical_address'
    #             | 'video_only' | 'assumed_home'
    assumed: bool


def decide_location(event: CalendarEvent, home_address: str) -> LocationDecision:
    """Classify an event's location with its reason — pure and deterministic."""
    if is_en_casa(event):
        return LocationDecision("home", "en_casa_marker", False)
    dest = physical_location(event)
    if dest is not None:
        if same_address(dest, home_address):
            return LocationDecision("home", "home_address", False)
        return LocationDecision("away", "physical_address", False)
    if event.video_link:
        return LocationDecision("home", "video_only", False)
    return LocationDecision("home", "assumed_home", True)


def location_kind(event: CalendarEvent, home_address: str) -> str:
    """Daily-scan location class: ``'home'`` | ``'away'``.

    At-home if ``(en casa)``, a video link with no physical address, or — since
    #168 — no location at all (assumed home; :func:`decide_location` carries the
    ``assumed`` flag so the assumption is surfaced, never silent). A physical
    address is 'away' unless it is the home address.
    """
    return decide_location(event, home_address).kind


# --------------------------------------------------------------- origin / commutes


def _origin_source_event(
    event: CalendarEvent,
    same_person_events: list[CalendarEvent],
    *,
    home_address: str,
    lookback_min: int,
) -> CalendarEvent | None:
    """The preceding commute event this event chains off, or None (⇒ from home).

    The latest same-person commute active now or ended within ``lookback_min``
    before this event's start (office → lunch chains).
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
        return None
    return max(candidates, key=lambda e: e.end)


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
    source = _origin_source_event(
        event, same_person_events, home_address=home_address, lookback_min=lookback_min
    )
    if source is None:
        return home_address
    return physical_location(source) or home_address


@dataclass(frozen=True)
class CommuteLeg:
    """One resolved commute to check for traffic.

    ``origin_event_end`` is the end time of the preceding event when the origin
    was chained off it (a back-to-back leg), else ``None`` (origin is home). It
    lets the traffic check judge whether the hop is *feasible at all* — travel
    time vs. the gap between the two events (#169, completing #168's deferred
    back-to-back adjacency judging).
    """

    person: str
    event: CalendarEvent
    origin: str
    destination: str
    origin_event_end: datetime | None = None


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
            source = _origin_source_event(
                event, events, home_address=home_address, lookback_min=origin_lookback_min
            )
            origin = home_address if source is None else (physical_location(source) or home_address)
            if same_address(origin, dest):
                continue
            legs.append(
                CommuteLeg(
                    person=person,
                    event=event,
                    origin=origin,
                    destination=dest,
                    origin_event_end=source.end if source is not None else None,
                )
            )
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

    kind: str  # 'coverage_gap' | 'impossible_overlap'
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


def day_windows(family: FamilyConfig, day: date) -> list[tuple[str, time, time]]:
    """The day's required childcare moments/windows as ``(label, start, end)``.

    Explicit childcare windows filtered to the weekday (a missing/invalid end
    degrades to the point-in-time deadline), plus the daily kids-home pickup on
    school weekdays. Shared by the calendar coverage check and the live
    presence-ETA assessment (#177) so both judge the same windows.
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
    return windows


def arrival_margin_min(now: datetime, eta_min: float, window_start: datetime) -> int:
    """Minutes of slack if the person leaves *now*: negative ⇒ they arrive late.

    The deterministic core of the live coverage judgment (#177):
    ``window_start - (now + eta)``, floored to whole minutes so a 30-second
    shortfall already reads as ``-1`` rather than rounding up to "just fine".
    """
    gap_min = (window_start - now).total_seconds() / 60.0
    return math.floor(gap_min - eta_min)


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
    windows = day_windows(family, day)
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


def find_missing_locations(
    events_by_person: dict[str, list[CalendarEvent]],
    *,
    home_address: str,
) -> list[tuple[str, CalendarEvent]]:
    """Timed events with no explicit location — assumed home, flagged to fix.

    Selects exactly the events :func:`decide_location` marks ``assumed``: the
    daily summary asks for their location so the assumption can be retired.
    """
    out: list[tuple[str, CalendarEvent]] = []
    for person, events in events_by_person.items():
        for event in events:
            if not event.all_day and decide_location(event, home_address).assumed:
                out.append((person, event))
    return out


def find_overlaps(
    events_by_person: dict[str, list[CalendarEvent]],
    *,
    home_address: str,
) -> list[Conflict]:
    """Same person needed in two different physical places at the same time.

    Flags strict time overlaps between a person's timed events whose resolved
    physical destinations differ — being in both is impossible, so this is a
    hard conflict regardless of childcare coverage. Deliberately conservative:
    events without a physical address (home, video, assumed home) never pair
    into an overlap, and exact back-to-back adjacency (``b.start == a.end``) is
    not an overlap. Judging whether the *travel* between two adjacent events is
    feasible needs live routing — that now lives in the traffic check, which
    routes each chained leg and flags it when travel time exceeds the gap (#169).
    """
    conflicts: list[Conflict] = []
    for person, events in events_by_person.items():
        placed = sorted(
            (e for e in events if not e.all_day and physical_location(e) is not None),
            key=lambda e: e.start,
        )
        for i, first in enumerate(placed):
            for second in placed[i + 1 :]:
                if second.start >= first.end:
                    break  # sorted by start: nothing later overlaps `first`
                first_dest = physical_location(first)
                second_dest = physical_location(second)
                if first_dest and second_dest and not same_address(first_dest, second_dest):
                    conflicts.append(
                        Conflict(
                            kind="impossible_overlap",
                            day=first.start.date().isoformat(),
                            detail=(
                                f"{person} is booked in two places at once: "
                                f"'{first.summary}' and '{second.summary}' overlap "
                                f"at {second.start.strftime('%H:%M')}"
                            ),
                        )
                    )
    return conflicts


def event_decisions(
    events_by_person: dict[str, list[CalendarEvent]],
    *,
    home_address: str,
) -> list[dict[str, object]]:
    """Per-event decision trace: every event in the window, with the why (#168).

    One record per event — person, raw location text, resolved kind, the source
    rule that decided it, and whether the home assumption was applied. All-day
    events are recorded but marked unassessed (the conflict math only weighs
    timed events). Pure data, JSON-ready, persisted in the run's summary.
    """
    records: list[dict[str, object]] = []
    for person, events in events_by_person.items():
        for event in sorted(events, key=lambda e: e.start):
            if event.all_day:
                records.append({
                    "person": person, "event": event.summary,
                    "start": event.start.isoformat(), "end": event.end.isoformat(),
                    "raw_location": event.location, "kind": "all_day",
                    "source": "not_assessed", "assumed": False,
                })
                continue
            decision = decide_location(event, home_address)
            records.append({
                "person": person, "event": event.summary,
                "start": event.start.isoformat(), "end": event.end.isoformat(),
                "raw_location": event.location,
                "video_link": bool(event.video_link),
                "kind": decision.kind, "source": decision.source,
                "assumed": decision.assumed,
                "commute": requires_commute(event, home_address),
            })
    return records
