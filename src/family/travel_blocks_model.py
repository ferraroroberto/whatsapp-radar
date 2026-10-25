"""Marker vocabulary and leg value types for auto-written commute travel blocks.

Pure, like :mod:`src.family.rules`: no I/O, no network, no Google client. This is
the bottom layer of :mod:`src.family.travel_blocks` — the reconcile
(:mod:`src.family.travel_blocks_reconcile`) and the pricing edge
(:mod:`src.family.travel_blocks_pricing`) build on it, and
:mod:`src.family.travel_blocks` re-exports every name here.

``calendar_readonly`` stays product-neutral and only transports the
``extendedProperties.private`` map; knowing that ``wr_travel_block`` means "we
wrote this" belongs here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from calendar_readonly.core import CalendarEvent

#: ``extendedProperties.private`` keys stamped on every block we write, and the
#: `privateExtendedProperty` filter the reconcile step queries with.
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

    Lets the reconcile step tell "unchanged" from "needs rewrite" by
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
