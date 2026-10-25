"""Existing-block state and the pure reconcile for commute travel blocks.

Still pure: everything here is a function of "what should exist" (the planned
legs from :mod:`src.family.travel_blocks_model`) and "what does exist" (blocks
handed in by the caller, fetched through the marker-scoped query). No client, no
filesystem, no network. :mod:`src.family.travel_blocks` re-exports every name
here.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.family.travel_blocks_model import (
    HASH_KEY,
    LEG_KEY,
    MARKER_FILTER,
    MARKER_KEY,
    MARKER_VALUE,
    SCHEMA_VERSION,
    SCHEMA_VERSION_KEY,
    SOURCE_EVENT_KEY,
    PlannedLeg,
    block_marker,
    leg_key,
)

logger = logging.getLogger(__name__)

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
PROTECT_REASON_BEYOND_HORIZON = "beyond_planning_horizon"  # seen only via the padded read
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
    listing deliberately reads further than the plan does. The two must
    not produce the same decision.
    """
    moment = block_start_moment(block)
    if moment is None:
        return PROTECT_REASON_START_UNKNOWN
    return PROTECT_REASON_BEYOND_HORIZON if moment >= plan_end else None


def _group_existing(
    existing: Sequence[ExistingBlock], protected_keys: frozenset[str]
) -> tuple[dict[str, list[ExistingBlock]], list[ProtectedBlock]]:
    """Existing blocks by leg key, with the unplanned-leg ones set aside as protected."""
    by_key: dict[str, list[ExistingBlock]] = {}
    protected_blocks: list[ProtectedBlock] = []
    for block in existing:
        if block.key in protected_keys:
            protected_blocks.append(ProtectedBlock(block, PROTECT_REASON_UNPLANNED_LEG))
            continue
        by_key.setdefault(block.key, []).append(block)
    return by_key, protected_blocks


def _sweep_orphans(
    by_key: Mapping[str, Sequence[ExistingBlock]],
    plan_end: datetime,
    deletes: list[PlannedDelete],
    protected_blocks: list[ProtectedBlock],
) -> None:
    """Delete blocks no desired leg wants, sparing those beyond the plan."""
    for orphans in by_key.values():
        for block in orphans:
            spared = _protect_reason_beyond_plan(block, plan_end)
            if spared is None:
                deletes.append(PlannedDelete(block, DELETE_REASON_ORPHANED))
            else:
                protected_blocks.append(ProtectedBlock(block, spared))


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
    ``existing`` deliberately reads **past** that moment (Calendar's
    ``timeMax`` is exclusive on an event's *start*, so a return block for an
    event ending after the horizon is otherwise never returned and gets
    re-inserted on every sweep). Widening the read must not widen the plan: a
    block found in that padded region has no desired counterpart simply because
    nothing was computed out there, and reading that absence as "orphaned"
    would delete correct blocks. Defaulting
    ``plan_end`` to "no window" would default that guard off, which is the one
    direction this feature may never fail in.

    ``protected`` names the leg keys whose desired shape this sweep **failed to
    establish** — every :class:`LegFailure` (see :func:`plan_travel_blocks`).
    Those keys are excluded from the diff entirely, on both sides: their blocks
    are neither re-added nor orphan-deleted, only reported in ``protected``.

    This is the module's one guard against a horizon-wide delete. A leg that
    failed to price is still *desired* — only its duration is missing — but
    `desired` omits it, so its existing block matches nothing and the orphan
    sweep below would delete it: one transient ``HTTP 429`` from Routes, or a
    sweep run after the drive's departure moment, would wipe the horizon's blocks
    and re-insert nothing. "We could not price it this minute" is not a delete
    trigger; only a cancelled source event or one that left the horizon is.
    Sibling of the :data:`FAILURE_BLOCKS_UNREADABLE` rule one layer up: an
    unestablished fact gets its own reported state and never the passing one.
    """
    protected_keys = frozenset(protected)
    by_key, protected_blocks = _group_existing(existing, protected_keys)

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
    _sweep_orphans(by_key, plan_end, deletes, protected_blocks)

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
