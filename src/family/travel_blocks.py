"""Desired-state planner for auto-written commute travel blocks (#265/#266, umbrella #263).

Everything above the "Routes pricing" divider is **pure**, like
:mod:`src.family.rules`: no I/O, no network, no Google client, no filesystem.
It is a function of the events already fetched plus a mapping of leg durations
supplied by the caller — so the calendar reconcile (step 3 of #263) can be
tested and reasoned about separately from *which blocks should exist*.

The computation is deliberately split in three:

1. :func:`desired_legs` — pure — decides which legs *should* exist and what each
   one must be priced for, without knowing any duration.
2. :func:`build_planned_legs` — pure — turns those requests into concrete
   time-boxed blocks once the durations are known, and resolves the one
   decision that genuinely needs them (the home-dwell chaining threshold).
3. :func:`price_legs` / :func:`plan_travel_blocks` (#266) — the module's only
   I/O — supply those durations from the live Routes API, with the route
   function injected so the whole sweep stays testable offline with a stub.

Nothing in this module writes to a calendar. Step 3 of #263 owns insert/delete;
until then a run produces a plan, a log and a payload, and touches nothing.

This module also owns the product-specific marker vocabulary. ``calendar_readonly``
stays product-neutral and only transports the ``extendedProperties.private`` map;
knowing that ``wr_travel_block`` means "we wrote this" belongs here.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol

import requests
from calendar_readonly.core import CalendarEvent

from src.config import Config
from src.family import rules
from src.traffic import RouteResult, TrafficReadError, compute_route

logger = logging.getLogger(__name__)

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


# --------------------------------------------------------------- Routes pricing (the I/O edge)
#
# Everything below this line talks to the network. The pure planner above never
# imports it back, so `desired_legs` / `build_planned_legs` stay testable with a
# plain dict of durations.

#: A leg was dropped because the Routes call failed, was rejected, or returned
#: no route. The fact could not be established — never a guessed duration.
FAILURE_ROUTES_ERROR = "routes_error"
#: A leg was dropped without calling Routes at all: its departure moment is in
#: the past. Routes prices only future departures (verified live: a past
#: ``departureTime`` is ``HTTP 400 "Timestamp must be set to a future time."``),
#: and a block for a drive that already happened reserves nothing anyway.
FAILURE_ANCHOR_IN_THE_PAST = "anchor_in_the_past"

STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_NO_ROUTES_API_KEY = "no_routes_api_key"
STATUS_NO_HOME_ADDRESS = "no_home_address"


class RouteFn(Protocol):
    """The slice of :func:`~src.traffic.compute_route` this module depends on.

    Injected rather than called directly so the sweep can be driven offline by a
    stub — the whole test suite prices legs without a network or an API key.
    """

    def __call__(
        self,
        origin: str,
        destination: str,
        *,
        api_key: str,
        departure_time: datetime | None = ...,
        session: requests.Session | None = ...,
    ) -> RouteResult: ...


@dataclass(frozen=True)
class LegFailure:
    """One leg that could not be priced, and why — its own reportable state.

    A dropped leg must never be indistinguishable from "no commute needed":
    silently writing nothing looks exactly like correctly deciding nothing was
    needed, which is the failure mode ``CLAUDE.md`` forbids. ``reason`` is the
    machine-readable discriminator (:data:`FAILURE_ROUTES_ERROR` /
    :data:`FAILURE_ANCHOR_IN_THE_PAST`); ``detail`` is the privacy-safe text —
    :class:`~src.traffic.TrafficReadError` messages never carry coordinates,
    addresses or API keys.
    """

    person: str
    calendar_id: str
    source_event_id: str
    leg: str
    reason: str
    detail: str


@dataclass(frozen=True)
class PricedLegs:
    """What one pricing sweep established: durations, non-facts, and its cost.

    ``routes_calls`` counts *attempted* Routes calls after within-sweep dedup —
    the number the per-leg billing is charged against. It is returned rather
    than derived by the caller so the count can never drift from what actually
    happened.
    """

    durations: dict[str, float]  # LegRequest.key -> minutes (unrounded)
    failures: list[LegFailure]
    routes_calls: int


def _departure_anchor(request: LegRequest) -> datetime:
    """The moment to price ``request`` as a *departure*.

    Routes v2 honours exactly one time field for ``DRIVE`` + ``TRAFFIC_AWARE``:
    ``departureTime``. Verified live against the API (#266) rather than taken
    from the docs — for one fixed pair of addresses a departure at 04:00
    returned 983 s and at 08:00 930 s, while *every* ``arrivalTime`` variant
    returned the depart-now baseline (1085 s) unchanged, including under
    ``TRAFFIC_AWARE_OPTIMAL``. An arrival-shaped call is therefore not a
    different estimate, it is silently no estimate at all — precisely what a
    07:00 sweep pricing a 09:00 school run must not get.

    A return leg's anchor already *is* a departure (the source event's end), so
    it is exact. An outbound leg's anchor is an arrival (the source event's
    start) and the true departure is one drive-length earlier — which is the
    number being computed, so it cannot be known before the call. Pricing the
    departure at the arrival moment costs one call and is wrong by at most the
    drive's own length (typically 10-30 min), against being wrong by the whole
    sweep-to-event distance (hours) if the moment were dropped. Refining it
    would take a second call per leg, which #266 explicitly rules out.
    """
    return request.anchor


def _call_key(request: LegRequest, anchor: datetime) -> tuple[str, str, str]:
    """Identity of the Routes call a leg needs — the within-sweep dedup key.

    Normalised to UTC whole minutes so two legs carrying different but
    equivalent tz offsets still share one call. Two people driving the same
    road at the same minute, or an outbound whose origin/destination/time
    coincide with another leg's, are one billable call, not two.
    """
    minute = anchor.astimezone(UTC).replace(second=0, microsecond=0)
    return (request.origin, request.destination, minute.isoformat())


def price_legs(
    leg_requests: Sequence[LegRequest],
    *,
    api_key: str,
    now: datetime,
    session: requests.Session | None = None,
    route_fn: RouteFn = compute_route,
) -> PricedLegs:
    """Price every leg with one live traffic-aware Routes call — the I/O edge.

    One call per distinct ``(origin, destination, departure minute)``: identical
    legs within a sweep share a call, and a *failed* call is cached too, so a
    repeated bad leg costs one attempt rather than one per occurrence.

    A failure degrades that leg alone — the loop never aborts the sweep — and is
    recorded as a :class:`LegFailure` instead of a duration, so
    :func:`build_planned_legs` drops the leg rather than emitting a guessed or
    zero-length block.
    """
    durations: dict[str, float] = {}
    failures: list[LegFailure] = []
    # Cached outcome per call key: minutes on success, None on failure (with the
    # detail alongside), so both halves of the dedup contract hold.
    outcomes: dict[tuple[str, str, str], tuple[float | None, str]] = {}
    routes_calls = 0

    for request in leg_requests:
        anchor = _departure_anchor(request)
        if anchor <= now:
            failures.append(
                _failure(
                    request,
                    FAILURE_ANCHOR_IN_THE_PAST,
                    "departure moment has already passed; Routes prices only future departures",
                )
            )
            continue
        key = _call_key(request, anchor)
        cached = outcomes.get(key)
        if cached is None:
            routes_calls += 1
            try:
                result = route_fn(
                    request.origin,
                    request.destination,
                    api_key=api_key,
                    departure_time=anchor,
                    session=session,
                )
            except TrafficReadError as exc:
                # Transport failure, non-200 (quota included) and an empty
                # `routes` list all arrive here, already phrased privacy-safely.
                cached = (None, str(exc))
            else:
                cached = (result.traffic_s / 60.0, "")
            outcomes[key] = cached
        minutes, detail = cached
        if minutes is None:
            failures.append(_failure(request, FAILURE_ROUTES_ERROR, detail))
            continue
        durations[request.key] = minutes

    return PricedLegs(durations=durations, failures=failures, routes_calls=routes_calls)


def _failure(request: LegRequest, reason: str, detail: str) -> LegFailure:
    return LegFailure(
        person=request.person,
        calendar_id=request.calendar_id,
        source_event_id=request.source_event_id,
        leg=request.leg,
        reason=reason,
        detail=detail,
    )


# --------------------------------------------------------------- the sweep


@dataclass(frozen=True)
class TravelBlockPlan:
    """One sweep's outcome: what should exist, and what could not be established.

    ``adds`` / ``deletes`` are the reconcile decision. At this step there is
    nothing on the calendar to diff against, so ``adds == legs`` and ``deletes``
    is always empty — the shape is already the final one, so step 3 of #263 only
    has to fill it in and no downstream reader changes then.

    ``event_summaries`` maps ``(calendar_id, event_id)`` to the source event's
    title. It is carried for reporting only, and is deliberately *not* a field of
    :class:`PlannedLeg`: a title must never be able to influence the plan or the
    content hash.
    """

    status: str
    dry_run: bool
    legs: list[PlannedLeg]
    adds: list[PlannedLeg]
    deletes: list[dict[str, Any]]
    failures: list[LegFailure]
    routes_calls: int
    horizon_start: datetime | None = None
    horizon_end: datetime | None = None
    event_summaries: dict[tuple[str, str], str] = field(default_factory=dict)

    def summary_of(self, calendar_id: str, source_event_id: str) -> str:
        return self.event_summaries.get((calendar_id, source_event_id), "")

    def to_payload(self) -> dict[str, Any]:
        """JSON-ready plan for the run payload — the rehearsal surface of #263.

        A non-``ok`` status carries the marker alone: there is no plan to report,
        and empty ``adds``/``failures`` lists next to ``"disabled"`` would read
        like a computed all-clear.
        """
        if self.status != STATUS_OK:
            return {"status": self.status}
        return {
            "status": self.status,
            "dry_run": self.dry_run,
            "horizon_start": _iso_or_none(self.horizon_start),
            "horizon_end": _iso_or_none(self.horizon_end),
            "routes_calls": self.routes_calls,
            "counts": {
                "desired": len(self.legs),
                "adds": len(self.adds),
                "deletes": len(self.deletes),
                "failures": len(self.failures),
            },
            "adds": [self._leg_payload(leg) for leg in self.adds],
            "deletes": list(self.deletes),
            "failures": [self._failure_payload(failure) for failure in self.failures],
        }

    def _leg_payload(self, leg: PlannedLeg) -> dict[str, Any]:
        return {
            "leg": leg.leg,
            "person": leg.person,
            "calendar_id": leg.calendar_id,
            "source_event_id": leg.source_event_id,
            "event": self.summary_of(leg.calendar_id, leg.source_event_id),
            "origin": leg.origin,
            "destination": leg.destination,
            "start": leg.start.isoformat(),
            "end": leg.end.isoformat(),
            "minutes": leg.minutes,
            "hash": leg.content_hash,
        }

    def _failure_payload(self, failure: LegFailure) -> dict[str, Any]:
        return {
            "leg": failure.leg,
            "person": failure.person,
            "calendar_id": failure.calendar_id,
            "source_event_id": failure.source_event_id,
            "event": self.summary_of(failure.calendar_id, failure.source_event_id),
            # `unpriced`, never `ok` and never absent: a leg we failed to price
            # has to look different from one that needed no block at all.
            "status": "unpriced",
            "reason": failure.reason,
            "detail": failure.detail,
        }


def _iso_or_none(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def plan_travel_blocks(
    config: Config,
    events_by_person: Mapping[str, Sequence[CalendarEvent]],
    *,
    now: datetime,
    session: requests.Session | None = None,
    route_fn: RouteFn = compute_route,
) -> TravelBlockPlan:
    """Plan the horizon's travel blocks from events the caller already fetched.

    Takes ``events_by_person`` rather than fetching: the daily sweep
    (:func:`~src.family.calendar_scan.run_calendar_scan`) has already read the
    window, and a second Calendar fetch would be a second read seam to keep the
    feedback-loop guard on.

    Gating, in order, each with its own reportable status and **zero** Routes
    calls: the feature off; no Routes API key; no configured home address (the
    committed default ships blank, and a plan built on it would reserve time for
    a drive to nowhere). None of these may be folded into a plain empty plan,
    which would read as "nothing to do".
    """
    family = config.family
    settings = family.travel_blocks
    if not settings.enabled:
        return _empty_plan(STATUS_DISABLED, settings.dry_run)
    if not config.traffic.api_key:
        return _empty_plan(STATUS_NO_ROUTES_API_KEY, settings.dry_run)
    if not family.home_address.strip():
        return _empty_plan(STATUS_NO_HOME_ADDRESS, settings.dry_run)

    # The same expression `run_calendar_scan` uses for its own fetch window, so
    # the horizon can never start outside the events this was handed.
    horizon_start = datetime.combine(now.date(), time.min).astimezone(now.tzinfo)
    horizon_days = settings.horizon_days or family.assessment_days
    horizon_end = horizon_start + timedelta(days=horizon_days)

    leg_requests = desired_legs(
        events_by_person,
        home_address=family.home_address,
        origin_lookback_min=config.traffic.origin_lookback_min,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        train_keywords=config.traffic.train_keywords,
    )
    # One pooled session for the whole sweep — a bare connection per leg would
    # park an ephemeral port in TIME_WAIT for every block priced.
    owned = session is None
    http = session or requests.Session()
    try:
        priced = price_legs(
            leg_requests,
            api_key=config.traffic.api_key,
            now=now,
            session=http,
            route_fn=route_fn,
        )
    finally:
        if owned:
            http.close()

    legs = build_planned_legs(
        leg_requests, priced.durations, min_home_dwell_min=settings.min_home_dwell_min
    )
    plan = TravelBlockPlan(
        status=STATUS_OK,
        dry_run=settings.dry_run,
        legs=legs,
        # Nothing to diff against yet (step 3 of #263 lists the calendar's own
        # marked blocks): every desired leg is a would-be add.
        adds=list(legs),
        deletes=[],
        failures=priced.failures,
        routes_calls=priced.routes_calls,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        event_summaries={
            (event.calendar_id, event.event_id): event.summary
            for events in events_by_person.values()
            for event in events
        },
    )
    log_plan(plan)
    return plan


def _empty_plan(status: str, dry_run: bool) -> TravelBlockPlan:
    return TravelBlockPlan(
        status=status,
        dry_run=dry_run,
        legs=[],
        adds=[],
        deletes=[],
        failures=[],
        routes_calls=0,
    )


def log_plan(plan: TravelBlockPlan, *, log: logging.Logger | None = None) -> None:
    """Log the complete plan at INFO — one line per leg, plus a counted summary.

    The per-leg lines are the rehearsal #263 asks for before any live write is
    allowed: origins, destinations, minutes and time boxes, readable straight
    from the run's output log. Unpriced legs are logged at WARNING with their
    reason so they can never be mistaken for legs that needed no block.
    """
    out = log or logger
    if plan.status != STATUS_OK:
        out.info("ℹ️ travel blocks: %s — no plan computed, no Routes calls", plan.status)
        return
    for leg in plan.adds:
        out.info(
            "ℹ️ travel block [add] %s %s — “%s”: %s → %s, %s-%s (%d min)",
            leg.person,
            leg.leg,
            plan.summary_of(leg.calendar_id, leg.source_event_id),
            leg.origin,
            leg.destination,
            leg.start.strftime("%a %d %b %H:%M"),
            leg.end.strftime("%H:%M"),
            leg.minutes,
        )
    for failure in plan.failures:
        out.warning(
            "⚠️ travel block unpriced (%s) %s %s — “%s”: %s. No block planned: this is a "
            "failure to establish the drive, not a decision that none is needed",
            failure.reason,
            failure.person,
            failure.leg,
            plan.summary_of(failure.calendar_id, failure.source_event_id),
            failure.detail,
        )
    out.info(
        "%s travel blocks: %d add(s), %d delete(s), %d unpriced leg(s), %d Routes call(s)%s",
        "⚠️" if plan.failures else "✅",
        len(plan.adds),
        len(plan.deletes),
        len(plan.failures),
        plan.routes_calls,
        " [dry-run]" if plan.dry_run else "",
    )
