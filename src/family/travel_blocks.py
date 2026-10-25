"""Desired-state planner + reconcile for auto-written commute travel blocks.

Everything here except the sweep's pricing call is **pure**, like
:mod:`src.family.rules`: no I/O, no network, no Google client, no filesystem.
It is a function of the events already fetched plus a mapping of leg durations
supplied by the caller — so the calendar reconcile can be
tested and reasoned about separately from *which blocks should exist*.

The planner is spread over four modules, each re-exported from here so callers
keep one import path: :mod:`src.family.travel_blocks_model` (marker vocabulary
and leg types), :mod:`src.family.travel_blocks_reconcile` (existing blocks and
the diff), :mod:`src.family.travel_blocks_pricing` (the Routes edge) and this
module (the desired-state planner and the sweep that ties them together).

The computation is deliberately split in four:

1. :func:`desired_legs` — pure — decides which legs *should* exist and what each
   one must be priced for, without knowing any duration.
2. :func:`build_planned_legs` — pure — turns those requests into concrete
   time-boxed blocks once the durations are known, and resolves the one
   decision that genuinely needs them (the home-dwell chaining threshold).
3. :func:`price_legs` / :func:`plan_travel_blocks` — the module's only
   I/O — supply those durations from the live Routes API, with the route
   function injected so the whole sweep stays testable offline with a stub.
4. :func:`reconcile` — pure again — diffs the desired legs against the
   blocks *already* on the calendar and produces the add/delete decision. It is
   told which leg keys are **protected** — the ones whose desired shape could
   not be established this sweep — and leaves their blocks strictly alone. It is
   also told where the plan *stops* (``plan_end``), because the listing that
   fetched those blocks deliberately reads further than the plan does
   (:func:`travel_block_listing_end`); a block out in that padded region is left
   alone too, for the same reason and reported the same way.

A failure to establish a fact must never be applied as a decision, so a leg that
failed to price travels into the reconcile as a protected key rather than being
dropped; :func:`reconcile` explains why.

Nothing in this module writes to a calendar: it computes what should exist,
what does exist, and the difference. :mod:`src.family.travel_blocks_write` owns
the whole write side — the insert, the marker-guarded delete and its backup —
so a delete can never be issued from the module that also decides the plan.

The product-specific marker vocabulary lives in
:mod:`src.family.travel_blocks_model`. ``calendar_readonly`` stays
product-neutral and only transports the ``extendedProperties.private`` map;
knowing that ``wr_travel_block`` means "we wrote this" belongs to this feature.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import requests
from calendar_readonly.core import CalendarEvent

from src.config import Config
from src.family import rules
from src.family.travel_blocks_model import (
    HASH_KEY,
    LEG_KEY,
    LEG_OUTBOUND,
    LEG_RETURN,
    MARKER_FILTER,
    MARKER_KEY,
    MARKER_VALUE,
    SCHEMA_VERSION,
    SCHEMA_VERSION_KEY,
    SOURCE_EVENT_KEY,
    LegRequest,
    PlannedLeg,
    block_marker,
    content_hash,
    is_travel_block,
    leg_key,
)
from src.family.travel_blocks_pricing import (
    FAILURE_ANCHOR_IN_THE_PAST,
    FAILURE_BLOCKS_UNREADABLE,
    FAILURE_ROUTES_ERROR,
    STATUS_DISABLED,
    STATUS_NO_HOME_ADDRESS,
    STATUS_NO_ROUTES_API_KEY,
    STATUS_OK,
    LegFailure,
    PricedLegs,
    RouteFn,
    price_legs,
)
from src.family.travel_blocks_reconcile import (
    _PROTECT_DETAIL,
    DELETE_REASON_DUPLICATE,
    DELETE_REASON_ORPHANED,
    DELETE_REASON_REPLACED,
    PROTECT_REASON_BEYOND_HORIZON,
    PROTECT_REASON_START_UNKNOWN,
    PROTECT_REASON_UNPLANNED_LEG,
    ExistingBlock,
    ExistingBlocks,
    PlannedDelete,
    ProtectedBlock,
    Reconciliation,
    block_start_moment,
    build_block_event,
    carries_marker,
    parse_existing_block,
    reconcile,
)
from src.traffic import compute_route

logger = logging.getLogger(__name__)

# The names defined in the sibling modules stay importable from here, so callers
# keep one import path for the whole planner.
__all__ = [
    "DELETE_REASON_DUPLICATE",
    "DELETE_REASON_ORPHANED",
    "DELETE_REASON_REPLACED",
    "FAILURE_ANCHOR_IN_THE_PAST",
    "FAILURE_BLOCKS_UNREADABLE",
    "FAILURE_ROUTES_ERROR",
    "HASH_KEY",
    "LEG_KEY",
    "LEG_OUTBOUND",
    "LEG_RETURN",
    "MARKER_FILTER",
    "MARKER_KEY",
    "MARKER_VALUE",
    "PROTECT_REASON_BEYOND_HORIZON",
    "PROTECT_REASON_START_UNKNOWN",
    "PROTECT_REASON_UNPLANNED_LEG",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_KEY",
    "SOURCE_EVENT_KEY",
    "STATUS_DISABLED",
    "STATUS_NO_HOME_ADDRESS",
    "STATUS_NO_ROUTES_API_KEY",
    "STATUS_OK",
    "ExistingBlock",
    "ExistingBlocks",
    "LegFailure",
    "LegRequest",
    "PlannedDelete",
    "PlannedLeg",
    "PricedLegs",
    "ProtectedBlock",
    "Reconciliation",
    "RouteFn",
    "TravelBlockPlan",
    "block_marker",
    "block_start_moment",
    "build_block_event",
    "build_planned_legs",
    "carries_marker",
    "chains_directly",
    "content_hash",
    "desired_legs",
    "empty_plan",
    "gate_status",
    "is_travel_block",
    "leg_key",
    "log_plan",
    "parse_existing_block",
    "plan_travel_blocks",
    "price_legs",
    "reconcile",
    "scan_window_days",
    "travel_block_horizon",
    "travel_block_listing_end",
]


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


def _outbound_requests(
    person: str,
    placed: Sequence[tuple[CalendarEvent, str]],
    person_events: list[CalendarEvent],
    *,
    home_address: str,
    origin_lookback_min: int,
) -> list[LegRequest]:
    """Outbound leg request for each placed event that needs a drive."""
    requests: list[LegRequest] = []
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
        requests.append(request)
    return requests


def _return_requests(
    person: str,
    placed: Sequence[tuple[CalendarEvent, str]],
    outbound_by_event: Mapping[str, LegRequest],
    *,
    home_address: str,
) -> list[LegRequest]:
    """Return-home candidates, each pointing at the next commuting event.

    The next event's own outbound block would cover the hop if the person
    chains straight on instead of going home. Note the adjacency is to the next
    *event*, not the next outbound: two events back-to-back at the same address
    produce no outbound between them, yet the person plainly does not go home
    either.
    """
    requests: list[LegRequest] = []
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

        outbound = _outbound_requests(
            person,
            placed,
            person_events,
            home_address=home_address,
            origin_lookback_min=origin_lookback_min,
        )
        requests.extend(outbound)
        outbound_by_event = {request.source_event_id: request for request in outbound}
        requests.extend(
            _return_requests(person, placed, outbound_by_event, home_address=home_address)
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
    zero or guessed length: an unpriceable leg is an unknown, and the pricing
    step owns reporting *why* the price is missing. Silently writing a zero-length block
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


# --------------------------------------------------------------- the sweep


@dataclass(frozen=True)
class TravelBlockPlan:
    """One sweep's outcome: what should exist, and what could not be established.

    ``legs`` is the desired state; ``adds`` / ``deletes`` / ``keeps`` /
    ``protected`` are the reconcile decision against what is already on the
    calendars. ``adds`` is a subset of ``legs``: a leg on a calendar whose
    current contents could not be read is deliberately *not* an add, and appears
    in ``failures`` instead.

    ``protected`` lists the existing blocks left untouched, each with the reason
    this sweep established nothing about it (see :func:`reconcile`). A
    :data:`PROTECT_REASON_UNPLANNED_LEG` entry always has a matching entry in
    ``failures``: the two together say "this block still exists and we do not
    know whether it is right", which is neither a keep nor a delete. A
    :data:`PROTECT_REASON_BEYOND_HORIZON` entry has no failure — nothing failed;
    the block simply lies past the window the plan covers, and was only seen
    because the listing reads further than the plan does.

    ``event_summaries`` maps ``(calendar_id, event_id)`` to the source event's
    title. It is carried for reporting only, and is deliberately *not* a field of
    :class:`PlannedLeg`: a title must never be able to influence the plan or the
    content hash.
    """

    status: str
    dry_run: bool
    legs: list[PlannedLeg]
    adds: list[PlannedLeg]
    deletes: list[PlannedDelete]
    failures: list[LegFailure]
    routes_calls: int
    keeps: list[ExistingBlock] = field(default_factory=list)
    protected: list[ProtectedBlock] = field(default_factory=list)
    horizon_start: datetime | None = None
    horizon_end: datetime | None = None
    event_summaries: dict[tuple[str, str], str] = field(default_factory=dict)

    def summary_of(self, calendar_id: str, source_event_id: str) -> str:
        return self.event_summaries.get((calendar_id, source_event_id), "")

    def to_payload(self) -> dict[str, Any]:
        """JSON-ready plan for the run payload — the rehearsal surface.

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
                "keeps": len(self.keeps),
                "protected": len(self.protected),
                "failures": len(self.failures),
            },
            "adds": [self._leg_payload(leg) for leg in self.adds],
            "deletes": [_delete_payload(pending) for pending in self.deletes],
            # Reported, not merely absent — and with the reason: "we left this
            # block alone" has to be readable off the run payload, or nobody can
            # tell it from "we checked and it was fine".
            "protected": [_protected_payload(pending) for pending in self.protected],
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


def _delete_payload(pending: PlannedDelete) -> dict[str, Any]:
    """One planned removal: the block, plus the reason it is stale.

    ``reason`` is what makes the entry reviewable — a delete list without one
    would be exactly the unaccountable output this feature must not produce.
    """
    return {"reason": pending.reason, **_block_payload(pending.block)}


def _protected_payload(pending: ProtectedBlock) -> dict[str, Any]:
    """One block left alone, plus which unestablished fact spared it.

    Shaped exactly like :func:`_delete_payload` — an entry that said only "left
    alone" would be as unreviewable as a delete with no reason.
    """
    return {"reason": pending.reason, **_block_payload(pending.block)}


def _block_payload(block: ExistingBlock) -> dict[str, Any]:
    """One existing block, named well enough to audit after the fact.

    No event title: the block's own summary is the configured template (it says
    nothing), and the *source* event's title is not knowable from the block.
    """
    return {
        "calendar_id": block.calendar_id,
        "event_id": block.event_id,
        "source_event_id": block.source_event_id,
        "leg": block.leg,
        "start": block.start,
        "hash": block.stored_hash,
        "schema_version": block.schema_version,
    }


def _iso_or_none(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def gate_status(config: Config) -> str | None:
    """The reason travel blocks must not run at all, or ``None`` to proceed.

    Split out of :func:`plan_travel_blocks` so the write-side orchestrator can
    answer the same question *before* spending a Calendar read on a feature that
    is off — which is the state of every default install. Each gate keeps its own
    reportable status; none may be folded into a plain empty plan, which would
    read as "nothing to do".
    """
    family = config.family
    if not family.travel_blocks.enabled:
        return STATUS_DISABLED
    if not config.traffic.api_key:
        return STATUS_NO_ROUTES_API_KEY
    if not family.home_address.strip():
        return STATUS_NO_HOME_ADDRESS
    return None


def travel_block_horizon(config: Config, now: datetime) -> tuple[datetime, datetime]:
    """The window the sweep maintains — the one expression, shared by both callers.

    Starts at :func:`src.family.rules.local_midnight` — literally the same
    function ``run_calendar_scan`` builds its own fetch window from, rather
    than a second copy of the same line — so the horizon can never start
    outside the events the planner was handed, nor outside the blocks the
    reconcile listed, for any ``now`` either of them is given.

    And it is **clamped** to that fetch window's length for the same reason
    (:func:`scan_window_days`). Once the reconcile can delete, a horizon longer
    than the events it was handed is not merely wasteful: every block beyond the
    fetched days would have no desired counterpart, be judged an orphan and be
    deleted on every single run — the exact opposite of the zero-writes-when-
    unchanged contract. Prefer clamping the knob to widening the scan.
    """
    family = config.family
    horizon_start = rules.local_midnight(now)
    horizon_days = family.travel_blocks.horizon_days or family.assessment_days
    return horizon_start, horizon_start + timedelta(
        days=min(horizon_days, scan_window_days(config))
    )


#: How far past the last block it could possibly have written the marker-scoped
#: listing still reads. Slack on top of the exact bound below, for blocks
#: written by an *earlier* sweep against a source event that has since moved
#: earlier — their start is derived from an event end nothing can recompute.
#: A day is generous against every real commute and costs only a wider read: a
#: block seen out there is never planned for and never deleted, only reported.
LISTING_PAD = timedelta(days=1)


def travel_block_listing_end(
    events_by_person: Mapping[str, Sequence[CalendarEvent]],
    *,
    horizon_start: datetime,
    horizon_end: datetime,
) -> datetime:
    """The ``timeMax`` the marker-scoped listing must use to see every block we wrote.

    Calendar's ``timeMax`` is **exclusive on an event's start**, so a listing
    that stopped exactly at ``horizon_end`` would miss blocks. A return block
    starts at its source event's *end*, so an evening event on the last horizon
    day that runs past local midnight has a block starting at or after
    ``horizon_end`` — never returned, never matched, re-inserted on every sweep
    until the horizon rolled forward.

    The bound is exact rather than a round number. Every block this sweep could
    have written for an in-horizon event starts either at ``event.start`` minus
    its drive (an outbound — necessarily *before* ``horizon_end``) or at
    ``event.end`` (a return). The drive's own length never enters it: the filter
    is on the block's start, not its end. So the latest possible start is the
    latest in-horizon source-event end, and :data:`LISTING_PAD` is added on top
    for the blocks a *previous* sweep wrote from an end that has since changed.

    All-day events and our own blocks are excluded for the same reasons
    :func:`desired_legs` excludes them: neither is ever a source event, so
    neither may stretch the window.

    This widens the **read only**. What is *desired* still spans
    ``[horizon_start, horizon_end)``, and :func:`reconcile` is told where that
    stops so a block out here is left alone rather than judged an orphan.
    """
    latest = horizon_end
    for events in events_by_person.values():
        for event in events:
            if event.all_day or is_travel_block(event):
                continue
            if horizon_start <= event.start < horizon_end and event.end > latest:
                latest = event.end
    return latest + LISTING_PAD


def scan_window_days(config: Config) -> int:
    """How many days of events the daily scan fetches — ``run_calendar_scan``'s own expression.

    Named here rather than duplicated as a literal because the travel-block
    horizon has to stay inside it; the two must move together.
    """
    family = config.family
    return max(family.unknown_scan_days, family.assessment_days)


def _price_sweep(
    config: Config,
    leg_requests: Sequence[LegRequest],
    *,
    now: datetime,
    session: requests.Session | None,
    route_fn: RouteFn,
) -> PricedLegs:
    # One pooled session for the whole sweep — a bare connection per leg would
    # park an ephemeral port in TIME_WAIT for every block priced.
    owned = session is None
    http = session or requests.Session()
    try:
        return price_legs(
            leg_requests,
            api_key=config.traffic.api_key,
            now=now,
            session=http,
            route_fn=route_fn,
        )
    finally:
        if owned:
            http.close()


def _unreadable_failures(
    legs: Sequence[PlannedLeg], existing: ExistingBlocks
) -> list[LegFailure]:
    """A failure per desired leg whose calendar's own blocks could not be listed."""
    return [
        LegFailure(
            person=leg.person,
            calendar_id=leg.calendar_id,
            source_event_id=leg.source_event_id,
            leg=leg.leg,
            reason=FAILURE_BLOCKS_UNREADABLE,
            detail=existing.unreadable[leg.calendar_id],
        )
        for leg in legs
        if leg.calendar_id in existing.unreadable
    ]


def plan_travel_blocks(
    config: Config,
    events_by_person: Mapping[str, Sequence[CalendarEvent]],
    *,
    now: datetime,
    existing: ExistingBlocks,
    session: requests.Session | None = None,
    route_fn: RouteFn = compute_route,
    dry_run: bool | None = None,
) -> TravelBlockPlan:
    """Plan and reconcile the horizon's travel blocks — the desired-state half.

    Takes ``events_by_person`` rather than fetching: the daily sweep
    (:func:`~src.family.calendar_scan.run_calendar_scan`) has already read the
    window, and a second Calendar fetch would be a second read seam to keep the
    feedback-loop guard on.

    ``existing`` is **required**, with no default. What is already on the
    calendar is exactly the fact an add/delete decision cannot be guessed at, and
    a defaulted "nothing" would silently re-insert the whole horizon on every
    sweep. A caller with genuinely nothing to diff against passes an empty
    :class:`ExistingBlocks` and says so.

    Gating, in order, each with its own reportable status and **zero** Routes
    calls: the feature off; no Routes API key; no configured home address (the
    committed default ships blank, and a plan built on it would reserve time for
    a drive to nowhere) — see :func:`gate_status`.

    ``dry_run`` overrides the configured mode for this one call and may only
    ever tighten it (:func:`~src.family.travel_blocks_write.run_travel_blocks`
    passes ``settings.dry_run or force_dry_run``). ``None`` — the default —
    means "whatever the config says", so every existing caller is unchanged.
    """
    family = config.family
    settings = family.travel_blocks
    planned_dry_run = settings.dry_run if dry_run is None else dry_run
    gate = gate_status(config)
    if gate is not None:
        return empty_plan(gate, planned_dry_run)

    horizon_start, horizon_end = travel_block_horizon(config, now)

    leg_requests = desired_legs(
        events_by_person,
        home_address=family.home_address,
        origin_lookback_min=config.traffic.origin_lookback_min,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        train_keywords=config.traffic.train_keywords,
    )
    priced = _price_sweep(
        config, leg_requests, now=now, session=session, route_fn=route_fn
    )

    legs = build_planned_legs(
        leg_requests, priced.durations, min_home_dwell_min=settings.min_home_dwell_min
    )
    # A calendar whose own blocks could not be listed is skipped whole: its legs
    # stay in `legs` (they are still desired) but become reportable failures
    # rather than adds, and no existing block of its can be a delete candidate
    # either, since none were fetched.
    reconcilable = [leg for leg in legs if leg.calendar_id not in existing.unreadable]
    # Every failed reason is protected (see `reconcile`), deliberately: re-running
    # the scan has to be safe at any hour.
    protected = {
        leg_key(failure.calendar_id, failure.source_event_id, failure.leg)
        for failure in priced.failures
    }
    # `plan_end` is the horizon's end, *not* the padded listing's (see `reconcile`).
    diff = reconcile(reconcilable, existing.blocks, plan_end=horizon_end, protected=protected)
    failures = list(priced.failures)
    failures.extend(_unreadable_failures(legs, existing))
    plan = TravelBlockPlan(
        status=STATUS_OK,
        dry_run=planned_dry_run,
        legs=legs,
        adds=diff.adds,
        deletes=diff.deletes,
        failures=failures,
        routes_calls=priced.routes_calls,
        keeps=diff.keeps,
        protected=diff.protected,
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


def empty_plan(status: str, dry_run: bool) -> TravelBlockPlan:
    """A gated, nothing-computed plan carrying only its reason."""
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

    The per-leg lines are the rehearsal required before any live write is
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
    for pending in plan.deletes:
        out.info(
            "ℹ️ travel block [delete: %s] %s on %s — %s leg of source event %s (starts %s)",
            pending.reason,
            pending.block.event_id,
            pending.block.calendar_id,
            pending.block.leg or "?",
            pending.block.source_event_id or "?",
            pending.block.start or "?",
        )
    for spared in plan.protected:
        block = spared.block
        out.info(
            "ℹ️ travel block [protected: %s] %s on %s — %s leg of source event %s (starts %s) "
            "left exactly as it is: %s, and a fact this sweep did not establish is never "
            "applied as a delete",
            spared.reason,
            block.event_id,
            block.calendar_id,
            block.leg or "?",
            block.source_event_id or "?",
            block.start or "?",
            _PROTECT_DETAIL.get(spared.reason, "nothing this sweep established says it is stale"),
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
        "%s travel blocks: %d add(s), %d delete(s), %d kept, %d left alone, %d unpriced leg(s), "
        "%d Routes call(s)%s",
        "⚠️" if plan.failures else "✅",
        len(plan.adds),
        len(plan.deletes),
        len(plan.keeps),
        len(plan.protected),
        len(plan.failures),
        plan.routes_calls,
        " [dry-run]" if plan.dry_run else "",
    )
