"""Routes pricing — the network edge of the commute travel-block planner.

Everything here talks to the Routes API through an injected route function. The
pure planner (:mod:`src.family.travel_blocks`) never needs this module's I/O to
test, so ``desired_legs`` / ``build_planned_legs`` stay testable with a plain dict
of durations. :mod:`src.family.travel_blocks` re-exports every name here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import requests

from src.family.travel_blocks_model import LegRequest
from src.traffic import RouteResult, TrafficReadError, compute_route

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
#: its calendar could not be listed. The current state being unknown, an
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

    ``calendar_id`` / ``source_event_id`` / ``leg`` let :func:`leg_key`
    reconstruct the failed leg's key, which :func:`plan_travel_blocks` hands
    ``reconcile`` as *protected*.
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

    Routes honours only ``departureTime`` for ``DRIVE`` (see
    :func:`~src.traffic.routes_client.compute_route`), so an arrival-shaped call
    would price a 09:00 school run at the 07:00 sweep's traffic.

    A return leg's anchor already *is* a departure (the source event's end), so
    it is exact. An outbound leg's anchor is an arrival (the source event's
    start) and the true departure is one drive-length earlier — which is the
    number being computed, so it cannot be known before the call. Pricing the
    departure at the arrival moment costs one call and is wrong by at most the
    drive's own length (typically 10-30 min), against being wrong by the whole
    sweep-to-event distance (hours) if the moment were dropped. Refining it
    would take a second call per leg, which the pricing design rules out.
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
