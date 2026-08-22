"""Desired-state planner for auto-written commute travel blocks (#265, umbrella #263).

Pure, like :mod:`src.family.rules`: no I/O, no network, no Google client, no
filesystem. Everything here is a function of the events already fetched plus a
mapping of leg durations supplied by the caller — so the Routes pricing (#266)
and the calendar reconcile (step 3 of #263) can be tested and reasoned about
separately from *which blocks should exist*.

The computation is deliberately split in two:

1. :func:`desired_legs` — pure — decides which legs *should* exist and what each
   one must be priced for, without knowing any duration.
2. :func:`build_planned_legs` — pure — turns those requests into concrete
   time-boxed blocks once the durations are known, and resolves the one
   decision that genuinely needs them (the home-dwell chaining threshold).

This module also owns the product-specific marker vocabulary. ``calendar_readonly``
stays product-neutral and only transports the ``extendedProperties.private`` map;
knowing that ``wr_travel_block`` means "we wrote this" belongs here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from calendar_readonly.core import CalendarEvent

from src.family import rules

# --------------------------------------------------------------- marker vocabulary

#: ``extendedProperties.private`` keys stamped on every block we write, and the
#: `privateExtendedProperty` filter the reconcile step (step 3 of #263) queries with.
MARKER_KEY = "wr_travel_block"
MARKER_VALUE = "1"
SOURCE_EVENT_KEY = "wr_source_event_id"
LEG_KEY = "wr_leg"
SCHEMA_VERSION_KEY = "wr_schema_version"
HASH_KEY = "wr_hash"

#: Bumped when the block *shape* changes in a way that must force a rewrite of
#: already-written blocks. It is part of :func:`content_hash`, so a bump makes
#: every existing block's stored hash mismatch and the reconcile re-issues it.
SCHEMA_VERSION = "1"

LEG_OUTBOUND = "outbound"
LEG_RETURN = "return"


def is_travel_block(event: CalendarEvent) -> bool:
    """True when this event is one of *our* travel blocks — the feedback-loop guard.

    A travel block is a real event on a real calendar: on the next sweep the
    read path hands it straight back, where it has a physical ``location``,
    passes :func:`~src.family.rules.requires_commute`, and would become both a
    commute leg of its own *and* a candidate origin for the following event.
    Left unguarded the sweep feeds on its own output.

    The guard is applied **once, at the read seam**
    (:func:`src.family.calendar_source.fetch_events_by_person`) rather than at
    each of the five consumers (``event_decisions``, ``find_missing_locations``,
    ``find_conflicts``, ``find_overlaps``, ``upcoming_commutes``). One filter
    cannot be forgotten by a future caller; five can. Do not "restore"
    per-consumer filtering — it would be redundant and would rot.

    The reconcile step still sees our blocks: it fetches them separately via
    ``events.list(privateExtendedProperty="wr_travel_block=1")``, which never
    goes through that seam.
    """
    return event.extended_private.get(MARKER_KEY) == MARKER_VALUE


# --------------------------------------------------------------- desired state


@dataclass(frozen=True)
class LegRequest:
    """One leg that *should* exist, still waiting for its duration.

    ``anchor`` is the moment the duration must be priced for, and its meaning
    differs by leg: for an outbound it is the *arrival* time (the source
    event's start), for a return it is the *departure* time (the source event's
    end). Traffic depends on the clock, so pricing a 09:00 arrival at 07:00 is
    the whole point of carrying it.

    ``next_gap_min`` / ``next_outbound_key`` are the adjacency facts a return
    leg needs for the home-dwell decision. They stay unresolved here because
    the threshold is a function of durations (see :func:`chains_directly`).
    ``next_gap_min is None`` means there is no following commuting event at
    all, so the person definitely goes home. A gap *with* a ``None``
    ``next_outbound_key`` is the A→A case — the next event is at this same
    destination, so chaining onto it costs no drive.
    """

    person: str
    calendar_id: str
    leg: str  # LEG_OUTBOUND | LEG_RETURN
    source_event_id: str
    origin: str
    destination: str
    anchor: datetime
    source_start: datetime
    source_end: datetime
    next_gap_min: float | None = None
    next_outbound_key: str | None = None

    @property
    def key(self) -> str:
        """Stable identity used to look a duration up in the ``durations`` mapping."""
        return leg_key(self.calendar_id, self.source_event_id, self.leg)


@dataclass(frozen=True)
class PlannedLeg:
    """A concrete, time-boxed block ready to be written (or diffed against)."""

    person: str
    calendar_id: str
    leg: str  # LEG_OUTBOUND | LEG_RETURN
    source_event_id: str
    origin: str
    destination: str
    start: datetime
    end: datetime
    source_start: datetime
    source_end: datetime
    minutes: int

    @property
    def key(self) -> str:
        return leg_key(self.calendar_id, self.source_event_id, self.leg)

    @property
    def content_hash(self) -> str:
        return content_hash(
            origin=self.origin,
            destination=self.destination,
            source_start=self.source_start,
            source_end=self.source_end,
            minutes=self.minutes,
        )


def leg_key(calendar_id: str, source_event_id: str, leg: str) -> str:
    """The one spelling of a leg's identity, shared by request and planned leg.

    ``calendar_id`` is part of it because two household members invited to the
    same event carry the same Google event id on their own calendars.
    """
    return f"{calendar_id}::{source_event_id}::{leg}"


def content_hash(
    *,
    origin: str,
    destination: str,
    source_start: datetime,
    source_end: datetime,
    minutes: int,
) -> str:
    """Stable digest of everything a written block encodes.

    Lets the reconcile step (step 3 of #263) tell "unchanged" from "needs rewrite" by
    comparing against the hash stamped on the existing block, without
    re-deriving the plan. ``sha256`` rather than :func:`hash` because it must be
    stable *across processes* — Python's string hashing is salted per run.
    :data:`SCHEMA_VERSION` is part of the payload so bumping it invalidates
    every previously-written block by construction.
    """
    payload = "\x1f".join([
        SCHEMA_VERSION,
        origin,
        destination,
        source_start.isoformat(),
        source_end.isoformat(),
        str(int(minutes)),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def block_marker(leg: PlannedLeg) -> dict[str, str]:
    """The ``extendedProperties.private`` map to stamp on ``leg``'s calendar event.

    Kept here rather than in the writer so the marker this module *recognizes*
    (:func:`is_travel_block`) and the marker it *emits* can never drift apart.
    """
    return {
        MARKER_KEY: MARKER_VALUE,
        SOURCE_EVENT_KEY: leg.source_event_id,
        LEG_KEY: leg.leg,
        SCHEMA_VERSION_KEY: SCHEMA_VERSION,
        HASH_KEY: leg.content_hash,
    }


def _drive_destination(
    event: CalendarEvent, *, home_address: str, train_keywords: Sequence[str]
) -> str | None:
    """Where ``event`` requires driving to, or ``None`` when it warrants no block.

    Layered on :func:`~src.family.rules.requires_commute` (which already rules
    out ``(en casa)``, video-only and no-location events, and the home address
    itself) with the two extra exclusions this feature needs: all-day events
    have no meaningful departure moment, and a train commute must not be given
    a DRIVE duration.

    Returning the address rather than a bool folds "is this a commute" and
    "where to" into one answer, so the planner never has to re-ask.
    """
    if event.all_day:
        return None
    if rules.is_train_commute(event, tuple(train_keywords)):
        return None
    if not rules.requires_commute(event, home_address):
        return None
    return rules.physical_location(event)


def desired_legs(
    events_by_person: Mapping[str, Sequence[CalendarEvent]],
    *,
    home_address: str,
    origin_lookback_min: int,
    horizon_start: datetime,
    horizon_end: datetime,
    train_keywords: Sequence[str] = (),
) -> list[LegRequest]:
    """Every leg that should exist in ``[horizon_start, horizon_end)`` — pure.

    Outbound legs are priced from :func:`~src.family.rules.resolve_origin`, so a
    chained A→B trip is priced A→B rather than home→B; that function stays the
    authority on the outbound side. Return legs are emitted as *candidates*
    carrying their adjacency facts — whether one survives depends on durations
    and is settled in :func:`build_planned_legs`.

    Events outside the horizon are still passed to ``resolve_origin`` as
    context (an event ending just before ``horizon_start`` is a legitimate
    origin) but never produce legs of their own.
    """
    if not home_address.strip():
        # Without a home address there is no origin for an outbound and no
        # destination for a return — a plan built on a blank one would reserve
        # time for a drive to nowhere. The committed default ships blank.
        return []
    requests: list[LegRequest] = []
    for person, events in events_by_person.items():
        # Belt-and-braces on top of the read-seam filter, and it has to happen
        # *here* rather than inside `_commutes`: `resolve_origin` reads the
        # whole per-person list, and a block left in it would be picked as the
        # preceding "commute" — pricing the next leg from its own destination.
        person_events = [event for event in events if not is_travel_block(event)]
        # `(event, destination)` for every in-horizon event that warrants a
        # block, in start order — the one pass that decides "is this a drive".
        placed: list[tuple[CalendarEvent, str]] = []
        for event in person_events:
            if not (horizon_start <= event.start < horizon_end):
                continue
            destination = _drive_destination(
                event, home_address=home_address, train_keywords=train_keywords
            )
            if destination is not None:
                placed.append((event, destination))
        placed.sort(key=lambda pair: pair[0].start)

        outbound_by_event: dict[str, LegRequest] = {}
        for event, destination in placed:
            origin = rules.resolve_origin(
                event,
                person_events,
                home_address=home_address,
                lookback_min=origin_lookback_min,
            )
            # A→A: already at the destination, nothing to drive.
            if rules.same_address(origin, destination):
                continue
            request = LegRequest(
                person=person,
                calendar_id=event.calendar_id,
                leg=LEG_OUTBOUND,
                source_event_id=event.event_id,
                origin=origin,
                destination=destination,
                anchor=event.start,  # an arrival time
                source_start=event.start,
                source_end=event.end,
            )
            outbound_by_event[event.event_id] = request
            requests.append(request)

        # Return legs are computed in a second pass so each one can point at the
        # *next* commuting event — whose own outbound block would cover the hop
        # if the person chains straight on instead of going home. Note the
        # adjacency is to the next *event*, not the next outbound: two events
        # back-to-back at the same address produce no outbound between them, yet
        # the person plainly does not go home either.
        for index, (event, destination) in enumerate(placed):
            next_event = next(
                (later for later, _ in placed[index + 1 :] if later.start >= event.end),
                None,
            )
            gap_min: float | None = None
            next_outbound: LegRequest | None = None
            if next_event is not None:
                gap_min = (next_event.start - event.end).total_seconds() / 60.0
                next_outbound = outbound_by_event.get(next_event.event_id)
            requests.append(
                LegRequest(
                    person=person,
                    calendar_id=event.calendar_id,
                    leg=LEG_RETURN,
                    source_event_id=event.event_id,
                    origin=destination,
                    destination=home_address,
                    anchor=event.end,  # a departure time
                    source_start=event.start,
                    source_end=event.end,
                    next_gap_min=gap_min,
                    next_outbound_key=None if next_outbound is None else next_outbound.key,
                )
            )
    return requests


# --------------------------------------------------------------- chaining threshold


def chains_directly(
    gap_min: float,
    drive_home_min: float,
    drive_out_min: float,
    min_home_dwell_min: int,
) -> bool:
    """True when the person hops straight A→B instead of going home in between.

    Going home and coming back out costs ``drive_home + drive_out`` and only
    buys ``gap - (drive_home + drive_out)`` minutes at home. Below
    ``min_home_dwell_min`` of that, the round trip is not worth anything, so
    assume the direct hop and write no return-home block — the next event's own
    outbound block, priced from this destination by ``resolve_origin``, already
    covers the drive.

    Strict ``<`` makes the boundary itself the "go home" case: exactly enough
    time for the drives plus the full dwell means the trip home is viable.
    """
    return gap_min < drive_home_min + drive_out_min + min_home_dwell_min


def build_planned_legs(
    leg_requests: Sequence[LegRequest],
    durations: Mapping[str, float],
    *,
    min_home_dwell_min: int,
) -> list[PlannedLeg]:
    """Turn priced leg requests into concrete blocks — pure.

    ``durations`` maps :attr:`LegRequest.key` to minutes. A request with no
    entry, or a non-positive one, is **dropped** rather than emitted with a
    zero or guessed length: an unpriceable leg is an unknown, and #266 owns
    reporting *why* the price is missing. Silently writing a zero-length block
    would be indistinguishable from correctly deciding no block was needed.
    """
    planned: list[PlannedLeg] = []
    for request in leg_requests:
        minutes = _minutes(durations, request.key)
        if minutes is None:
            continue
        if request.leg == LEG_RETURN and _is_chained(
            request, durations, minutes, min_home_dwell_min
        ):
            continue
        if request.leg == LEG_OUTBOUND:
            start, end = request.anchor - timedelta(minutes=minutes), request.anchor
        else:
            start, end = request.anchor, request.anchor + timedelta(minutes=minutes)
        planned.append(
            PlannedLeg(
                person=request.person,
                calendar_id=request.calendar_id,
                leg=request.leg,
                source_event_id=request.source_event_id,
                origin=request.origin,
                destination=request.destination,
                start=start,
                end=end,
                source_start=request.source_start,
                source_end=request.source_end,
                minutes=minutes,
            )
        )
    planned.sort(key=lambda p: (p.person, p.start, p.leg))
    return planned


def _minutes(durations: Mapping[str, float], key: str) -> int | None:
    """Whole minutes for ``key``, or ``None`` when unpriced or non-positive."""
    raw = durations.get(key)
    if raw is None:
        return None
    minutes = int(round(raw))
    return minutes if minutes > 0 else None


def _is_chained(
    request: LegRequest,
    durations: Mapping[str, float],
    drive_home_min: int,
    min_home_dwell_min: int,
) -> bool:
    """Whether this return-home candidate is superseded by a direct A→B hop.

    No following commuting event ⇒ never chained: nothing else would cover the
    drive. An *unpriced* following outbound is treated the same way — that
    block is about to be dropped too, so dropping this one as well would leave
    the drive unrepresented entirely. A following event at this very address
    costs no drive at all, hence ``drive_out = 0``.
    """
    if request.next_gap_min is None:
        return False
    drive_out_min = 0.0
    if request.next_outbound_key is not None:
        priced = _minutes(durations, request.next_outbound_key)
        if priced is None:
            return False
        drive_out_min = float(priced)
    return chains_directly(
        request.next_gap_min, drive_home_min, drive_out_min, min_home_dwell_min
    )
