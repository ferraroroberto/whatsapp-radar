"""Desired-state planner + reconcile for auto-written commute travel blocks (#263).

Everything above the "Routes pricing" divider is **pure**, like
:mod:`src.family.rules`: no I/O, no network, no Google client, no filesystem.
It is a function of the events already fetched plus a mapping of leg durations
supplied by the caller — so the calendar reconcile (step 3 of #263) can be
tested and reasoned about separately from *which blocks should exist*.

The computation is deliberately split in four:

1. :func:`desired_legs` — pure — decides which legs *should* exist and what each
   one must be priced for, without knowing any duration.
2. :func:`build_planned_legs` — pure — turns those requests into concrete
   time-boxed blocks once the durations are known, and resolves the one
   decision that genuinely needs them (the home-dwell chaining threshold).
3. :func:`price_legs` / :func:`plan_travel_blocks` (#266) — the module's only
   I/O — supply those durations from the live Routes API, with the route
   function injected so the whole sweep stays testable offline with a stub.
4. :func:`reconcile` (#267) — pure again — diffs the desired legs against the
   blocks *already* on the calendar and produces the add/delete decision. It is
   told which leg keys are **protected** — the ones whose desired shape could
   not be established this sweep — and leaves their blocks strictly alone. It is
   also told where the plan *stops* (``plan_end``), because the listing that
   fetched those blocks deliberately reads further than the plan does (#272,
   :func:`travel_block_listing_end`); a block out in that padded region is left
   alone too, for the same reason and reported the same way.

That last word is load-bearing, and the reason it is: a leg that failed to price
is still *desired*; all that is missing is its duration. Dropping it from the
desired set without saying so would make it indistinguishable from a leg that
must not exist, and the very next step — orphan deletion — would erase a whole
horizon of correct blocks because Routes answered ``HTTP 429`` once. A failure
to establish a fact must never be applied as a decision, so the failure travels
into the reconcile as a protected key rather than being silently dropped.

Nothing in this module writes to a calendar: it computes what should exist,
what does exist, and the difference. :mod:`src.family.travel_blocks_write` owns
the whole write side — the insert, the marker-guarded delete and its backup —
so a delete can never be issued from the module that also decides the plan.

This module also owns the product-specific marker vocabulary. ``calendar_readonly``
stays product-neutral and only transports the ``extendedProperties.private`` map;
knowing that ``wr_travel_block`` means "we wrote this" belongs here.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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

#: The ``privateExtendedProperty`` filter the reconcile lists with. Server-side,
#: so a human's event is never even fetched — the first line of defence for a
#: feature that can delete, and the reason it belongs at the query rather than
#: in a post-filter. ``calendar_readonly`` transports it without knowing it.
MARKER_FILTER = f"{MARKER_KEY}={MARKER_VALUE}"

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


# --------------------------------------------------------------- existing state & reconcile
#
# Still pure. Everything here is a function of "what should exist" (the planned
# legs above) and "what does exist" (blocks handed in by the caller, fetched
# through the marker-scoped query). No client, no filesystem, no network.

#: Why a block is being removed. Reported per delete so the log and the payload
#: can never say "deleted" without saying what made it stale.
DELETE_REASON_REPLACED = "replaced"  # a desired leg exists, but its hash/schema differs
DELETE_REASON_ORPHANED = "orphaned"  # no desired leg — source cancelled, moved or out of horizon
DELETE_REASON_DUPLICATE = "duplicate"  # a second block for a leg already satisfied by another

#: Why a block was left exactly as it is instead of being removed. Reported per
#: block for the same reason a delete carries a reason: "left alone" without a
#: why is as unaccountable as "deleted" without one, and the three cases are
#: genuinely different facts about what this sweep managed to establish.
PROTECT_REASON_UNPLANNED_LEG = "leg_not_planned"  # its leg could not be planned this sweep
PROTECT_REASON_BEYOND_HORIZON = "beyond_planning_horizon"  # seen only via the padded read (#272)
PROTECT_REASON_START_UNKNOWN = "start_not_established"  # cannot tell which side of the plan it is

_PROTECT_DETAIL = {
    PROTECT_REASON_UNPLANNED_LEG: (
        "its leg could not be planned this sweep, so nothing is known about whether it is "
        "still right"
    ),
    PROTECT_REASON_BEYOND_HORIZON: (
        "it starts past the planning horizon, where the sweep computes no desired state, so "
        "'no desired counterpart' says nothing about it"
    ),
    PROTECT_REASON_START_UNKNOWN: (
        "its start could not be read, so it cannot be placed inside or outside the planning "
        "horizon"
    ),
}


@dataclass(frozen=True)
class ExistingBlock:
    """One block *we* already wrote, as read back from the calendar.

    ``resource`` is the complete fetched event resource, kept verbatim for two
    reasons that both matter more than tidiness: it is what the pre-delete
    backup persists, and it is what the delete guard re-checks the marker on.
    Carrying the resource (rather than a bare event id) is what makes
    :meth:`src.family.travel_blocks_write.TravelBlockWriter.delete_block`
    *able* to refuse — an id alone would leave nothing to verify.

    Excluded from equality so two parses of the same block compare equal and
    the record stays hashable.
    """

    calendar_id: str
    event_id: str
    source_event_id: str
    leg: str
    schema_version: str
    stored_hash: str
    start: str = ""
    resource: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> str:
        """Same identity as the planned leg's — the join column of the diff."""
        return leg_key(self.calendar_id, self.source_event_id, self.leg)


@dataclass(frozen=True)
class ExistingBlocks:
    """What the reconcile knows about the calendars' current contents.

    ``unreadable`` maps a calendar id to why its marked-block listing failed.
    Such a calendar is **not** "empty": planning adds against an unknown current
    state would duplicate every block on it. It gets its own reportable failure
    (:data:`FAILURE_BLOCKS_UNREADABLE`) and is left entirely alone for the run —
    the ``CLAUDE.md`` rule that an unestablished fact is never folded into a
    passing one, applied to the riskiest place it could be folded.
    """

    blocks: tuple[ExistingBlock, ...] = ()
    unreadable: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedDelete:
    """One existing block the reconcile wants gone, and the reason it is stale."""

    block: ExistingBlock
    reason: str


@dataclass(frozen=True)
class ProtectedBlock:
    """One existing block left exactly as it is, and the reason it was not judged.

    Deliberately shaped like :class:`PlannedDelete`: both are "a block plus why",
    and a protection that could not say *which* unestablished fact spared it
    would be the silent pass ``CLAUDE.md`` forbids. ``reason`` is one of the
    ``PROTECT_REASON_*`` constants.
    """

    block: ExistingBlock
    reason: str


@dataclass(frozen=True)
class Reconciliation:
    """The four-way diff: what to insert, what to remove, and what to leave alone.

    ``keeps`` is the whole point of the exercise. Identical desired state must
    cost **zero** API writes, so an unchanged block is neither re-inserted nor
    touched — it is only counted.

    ``protected`` is the *other* kind of "leave alone": a block about which this
    sweep established nothing — either its leg could not be planned, or it sits
    outside the window the plan was computed for. It is reported rather than
    acted on — never a keep (nothing verified it) and never a delete (nothing
    said it was stale).
    """

    adds: list[PlannedLeg]
    deletes: list[PlannedDelete]
    keeps: list[ExistingBlock]
    protected: list[ProtectedBlock] = field(default_factory=list)


def parse_existing_block(raw: Mapping[str, Any], *, calendar_id: str) -> ExistingBlock | None:
    """Read one fetched resource back into an :class:`ExistingBlock`, or ``None``.

    Refuses anything without our marker even though the listing query already
    filtered on it: this is the only constructor of the record the delete path
    accepts, so making it marker-checking means an unmarked resource can never
    reach a delete decision in the first place. A resource with no id is
    likewise refused — there would be nothing to delete or to name in a backup.
    """
    private = _private_properties(raw)
    if private.get(MARKER_KEY) != MARKER_VALUE:
        logger.warning(
            "⚠️ travel blocks: ignoring calendar event %r returned by the %s query without "
            "the marker — it is not ours and will never be touched",
            raw.get("id"),
            MARKER_FILTER,
        )
        return None
    event_id = str(raw.get("id") or "")
    if not event_id:
        logger.warning("⚠️ travel blocks: ignoring a marked event with no id")
        return None
    start = raw.get("start") or {}
    return ExistingBlock(
        calendar_id=calendar_id,
        event_id=event_id,
        source_event_id=private.get(SOURCE_EVENT_KEY, ""),
        leg=private.get(LEG_KEY, ""),
        schema_version=private.get(SCHEMA_VERSION_KEY, ""),
        stored_hash=private.get(HASH_KEY, ""),
        start=str(start.get("dateTime") or start.get("date") or "")
        if isinstance(start, Mapping)
        else "",
        resource=dict(raw),
    )


def _private_properties(raw: Mapping[str, Any]) -> dict[str, str]:
    """``extendedProperties.private`` as ``{str: str}``; ``{}`` when absent or malformed."""
    node = raw.get("extendedProperties")
    private = node.get("private") if isinstance(node, Mapping) else None
    if not isinstance(private, Mapping):
        return {}
    return {str(key): str(value) for key, value in private.items()}


def carries_marker(raw: Mapping[str, Any]) -> bool:
    """Whether a raw event resource carries our marker — the delete guard's predicate.

    The resource-level twin of :func:`is_travel_block` (which reads a normalized
    :class:`~calendar_readonly.core.CalendarEvent`). Both spell the same rule
    once, here, next to the marker constants they check.
    """
    return _private_properties(raw).get(MARKER_KEY) == MARKER_VALUE


def block_start_moment(block: ExistingBlock) -> datetime | None:
    """``block``'s start as an aware datetime, or ``None`` when it cannot be established.

    Every block this app writes carries a ``dateTime`` start with an offset, so
    the ``None`` branches are the can't-happen ones — an all-day ``date`` start,
    an absent start, an unparseable string. They are still answered as ``None``
    rather than coerced to a moment, because the one caller uses this to decide
    whether a block may be *deleted*: a guessed position is exactly the kind of
    unestablished fact that must not be folded into a decision.
    """
    raw = block.start
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # A naive moment (an all-day `date`, or a start without an offset) cannot be
    # compared with the aware horizon at all — comparing would raise, and
    # assuming a timezone would invent the very fact that is missing.
    return moment if moment.tzinfo is not None else None


def _protect_reason_beyond_plan(block: ExistingBlock, plan_end: datetime) -> str | None:
    """Why ``block`` must be spared the orphan sweep, or ``None`` when it is fair game.

    Only ever consulted for a block with **no desired counterpart**. Inside the
    planning window that means "nothing wants this any more"; at or after
    ``plan_end`` it means nothing was ever computed about it, because the
    listing deliberately reads further than the plan does (#272). The two must
    not produce the same decision.
    """
    moment = block_start_moment(block)
    if moment is None:
        return PROTECT_REASON_START_UNKNOWN
    return PROTECT_REASON_BEYOND_HORIZON if moment >= plan_end else None


def reconcile(
    desired: Sequence[PlannedLeg],
    existing: Sequence[ExistingBlock],
    *,
    plan_end: datetime,
    protected: Collection[str] = (),
) -> Reconciliation:
    """Diff desired against existing on ``(calendar, source event, leg)`` — pure.

    Matching is on the leg key and *equality* is on the stored content hash, so:

    * unchanged block → kept, zero writes;
    * hash differs (the source event moved, or its location changed) → the old
      block is deleted and the new one inserted, because Calendar's ``patch``
      would leave a half-updated block behind if it failed midway;
    * unrecognised :data:`SCHEMA_VERSION` → treated as changed, never as a keep.
      A build that cannot vouch for a block's shape must not certify it;
    * no desired counterpart, and it starts **inside** the planning window →
      deleted (source cancelled, or out of horizon);
    * no desired counterpart, but it starts at or after ``plan_end`` → left
      strictly alone, reported as :data:`PROTECT_REASON_BEYOND_HORIZON`;
    * a duplicate for an already-satisfied leg → deleted. Duplicates should not
      happen, but a run interrupted between insert and its next sweep can leave
      one, and quietly tolerating it would let them accumulate forever.

    ``plan_end`` is where the *desired* state stops being computed — the
    planning horizon's end — and it is required, with no default, for the same
    reason ``existing`` is required one layer up. The listing that produced
    ``existing`` deliberately reads **past** that moment (#272: Calendar's
    ``timeMax`` is exclusive on an event's *start*, so a return block for an
    event ending after the horizon is otherwise never returned and gets
    re-inserted on every sweep). Widening the read must not widen the plan: a
    block found in that padded region has no desired counterpart simply because
    nothing was computed out there, and reading that absence as "orphaned"
    would turn a fix for duplicates into a delete of correct blocks. Defaulting
    ``plan_end`` to "no window" would default that guard off, which is the one
    direction this feature may never fail in.

    ``protected`` names the leg keys whose desired shape this sweep **failed to
    establish** — every :class:`LegFailure` (see :func:`plan_travel_blocks`).
    Those keys are excluded from the diff entirely, on both sides: their blocks
    are neither re-added nor orphan-deleted, only reported in ``protected``.

    Do not "simplify" that away. Without it, `desired` silently omits an
    unpriceable leg, its existing block matches nothing, and the orphan sweep
    below deletes it — so one transient ``HTTP 429`` from Routes, or a sweep run
    after the drive's departure moment, wipes the horizon's blocks and
    re-inserts nothing. The delete triggers of #267 are "the source event was
    cancelled or has left the horizon"; "we could not price it this minute" is
    not one of them, and a second sweep later the same day must never remove the
    blocks the morning's sweep correctly wrote. This is the sibling of the
    :data:`FAILURE_BLOCKS_UNREADABLE` rule one layer up: an unestablished fact
    gets its own reported state and never the passing one.
    """
    protected_keys = frozenset(protected)
    by_key: dict[str, list[ExistingBlock]] = {}
    protected_blocks: list[ProtectedBlock] = []
    for block in existing:
        if block.key in protected_keys:
            protected_blocks.append(ProtectedBlock(block, PROTECT_REASON_UNPLANNED_LEG))
            continue
        by_key.setdefault(block.key, []).append(block)

    adds: list[PlannedLeg] = []
    deletes: list[PlannedDelete] = []
    keeps: list[ExistingBlock] = []
    for leg in desired:
        # Belt and braces: a key cannot be both planned and failed today (one
        # leg request yields a duration or a failure, never both), but if the
        # two ever disagree the protected reading wins — leaving a calendar
        # alone is the only choice that cannot destroy anything.
        if leg.key in protected_keys:
            continue
        candidates = by_key.pop(leg.key, [])
        match = next(
            (
                block
                for block in candidates
                if block.schema_version == SCHEMA_VERSION and block.stored_hash == leg.content_hash
            ),
            None,
        )
        if match is None:
            adds.append(leg)
            deletes.extend(PlannedDelete(block, DELETE_REASON_REPLACED) for block in candidates)
            continue
        keeps.append(match)
        deletes.extend(
            PlannedDelete(block, DELETE_REASON_DUPLICATE)
            for block in candidates
            if block is not match
        )
    for orphans in by_key.values():
        for block in orphans:
            spared = _protect_reason_beyond_plan(block, plan_end)
            if spared is None:
                deletes.append(PlannedDelete(block, DELETE_REASON_ORPHANED))
            else:
                protected_blocks.append(ProtectedBlock(block, spared))

    deletes.sort(key=lambda pending: (pending.block.calendar_id, pending.block.start,
                                      pending.block.event_id))
    protected_blocks.sort(
        key=lambda pending: (pending.block.calendar_id, pending.block.start, pending.block.event_id)
    )
    return Reconciliation(
        adds=adds, deletes=deletes, keeps=keeps, protected=protected_blocks
    )


def build_block_event(leg: PlannedLeg, *, title_template: str) -> dict[str, Any]:
    """The Calendar event resource to insert for ``leg``.

    Deliberate choices, each of them a requirement rather than a preference:

    * ``summary`` is the configured title verbatim and never the destination —
      a shared calendar view must leak nothing about where the person is going.
    * ``location`` **is** the destination address: tapping the block in Google
      Calendar has to open navigation, which is most of the feature's value.
      (``visibility: "private"`` is what keeps that from being shared.)
    * ``opaque`` so the block actually defends the time as busy.
    * reminders explicitly overridden to none — these are placeholders around
      real events, and a notification for each one would be unusable.
    """
    return {
        "summary": title_template,
        "location": leg.destination,
        "start": {"dateTime": leg.start.isoformat()},
        "end": {"dateTime": leg.end.isoformat()},
        "transparency": "opaque",
        "visibility": "private",
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {"private": block_marker(leg)},
    }


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
#:
#: It reserves nothing, but it is also not *wrong*, and an afternoon sweep must
#: not undo the morning's work: a block whose travel time has already passed is
#: protected like any other unplannable leg and simply left where it is. That
#: keeps re-running the scan unconditionally safe — the property the whole
#: feature rests on — and there is deliberately no knob and no ``elapsed``
#: delete reason to turn it back into a delete.
FAILURE_ANCHOR_IN_THE_PAST = "anchor_in_the_past"
#: A leg was priced but *not* planned as an add, because the blocks already on
#: its calendar could not be listed (#267). The current state being unknown, an
#: add would risk duplicating a block that is already there — so the calendar is
#: left untouched for this run and the leg says so out loud.
FAILURE_BLOCKS_UNREADABLE = "existing_blocks_unreadable"

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

    ``calendar_id`` / ``source_event_id`` / ``leg`` are carried so
    :func:`leg_key` can reconstruct the exact key of the leg that failed. That
    key is what :func:`plan_travel_blocks` hands ``reconcile`` as *protected*,
    which is what stops an unpriceable leg's existing block from being read as
    an orphan and deleted. They are not decoration: drop them and a transient
    Routes outage becomes a horizon-wide delete.
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

    ``legs`` is the desired state; ``adds`` / ``deletes`` / ``keeps`` /
    ``protected`` are the reconcile decision against what is already on the
    calendars (#267). ``adds`` is a subset of ``legs``: a leg on a calendar whose
    current contents could not be read is deliberately *not* an add, and appears
    in ``failures`` instead.

    ``protected`` lists the existing blocks left untouched, each with the reason
    this sweep established nothing about it (see :func:`reconcile`). A
    :data:`PROTECT_REASON_UNPLANNED_LEG` entry always has a matching entry in
    ``failures``: the two together say "this block still exists and we do not
    know whether it is right", which is neither a keep nor a delete. A
    :data:`PROTECT_REASON_BEYOND_HORIZON` entry has no failure — nothing failed;
    the block simply lies past the window the plan covers, and was only seen
    because the listing reads further than the plan does (#272).

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
    function ``run_calendar_scan`` builds its own fetch window from since #280,
    rather than a second copy of the same line — so the horizon can never start
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
#: listing still reads (#272). Slack on top of the exact bound below, for blocks
#: written by an *earlier* sweep against a source event that has since moved
#: earlier — their start is derived from an event end nothing can recompute now.
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

    Calendar's ``timeMax`` is **exclusive on an event's start**, and the listing
    used to stop exactly at ``horizon_end``. A return block starts at its source
    event's *end*, so an evening event on the last horizon day that runs past
    local midnight has a block starting at or after ``horizon_end`` — never
    returned, never matched, re-inserted on every sweep until the horizon rolled
    forward (#272).

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
    # A calendar whose own blocks could not be listed is skipped whole: its legs
    # stay in `legs` (they are still desired) but become reportable failures
    # rather than adds, and no existing block of its can be a delete candidate
    # either, since none were fetched.
    reconcilable = [leg for leg in legs if leg.calendar_id not in existing.unreadable]
    # Every leg we failed to price is still *desired* — only its duration is
    # missing — so `build_planned_legs` dropping it must not read as "this block
    # is no longer wanted". Its key goes to the reconcile as protected: the block
    # already on the calendar is left exactly where it is, and reported. Without
    # this, a single `HTTP 429` from Routes (or an afternoon sweep whose morning
    # drives have already departed) turns every one of those blocks into an
    # orphan and deletes the horizon. Applies to *all* reasons deliberately:
    # re-running the scan has to be safe at any hour, always.
    protected = {
        leg_key(failure.calendar_id, failure.source_event_id, failure.leg)
        for failure in priced.failures
    }
    # `plan_end` is the horizon's end, *not* the listing's: the read is padded
    # past it (#272) precisely so the reconcile can see a block it wrote for an
    # event that runs past midnight — and the padding must not make those extra
    # blocks look orphaned, which is what telling the diff where the plan stops
    # prevents.
    diff = reconcile(reconcilable, existing.blocks, plan_end=horizon_end, protected=protected)
    failures = list(priced.failures)
    failures.extend(
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
    )
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
