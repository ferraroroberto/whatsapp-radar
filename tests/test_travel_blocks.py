"""Unit tests for the pure travel-block planner and its guards (issue #265).

Offline and deterministic: no Calendar client, no Routes call, no network.
Durations are injected as a plain mapping, exactly as #266 will inject the real
priced ones. All fixture people, addresses and calendar ids are invented.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from calendar_readonly.core import CalendarEvent, normalize_event

from src.config import CalendarAccount, CalendarConfig
from src.family import calendar_source, travel_blocks
from src.family.travel_blocks import (
    LEG_OUTBOUND,
    LEG_RETURN,
    LegRequest,
    PlannedLeg,
    build_planned_legs,
    chains_directly,
    content_hash,
    desired_legs,
    is_travel_block,
)

HOME = "1 Example Street, Sample Town"
OFFICE = "3 Example Road, Sample City"
CLINIC = "4 Example Lane, Sample Town"

PERSON = "parent-a"
CALENDAR_ID = "parent-a@example.test"

DAY = datetime(2026, 7, 20, tzinfo=UTC)
HORIZON_START = DAY
HORIZON_END = DAY + timedelta(days=2)
TRAIN_KEYWORDS = ("tren", "train")


def _at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def _event(
    *,
    eid: str,
    location: str = OFFICE,
    summary: str = "Appointment",
    start: datetime | None = None,
    end: datetime | None = None,
    all_day: bool = False,
    extended_private: dict[str, str] | None = None,
) -> CalendarEvent:
    start = start or _at(9)
    return CalendarEvent(
        event_id=eid,
        calendar_id=CALENDAR_ID,
        summary=summary,
        location=location,
        description="",
        start=start,
        end=end or (start + timedelta(hours=1)),
        all_day=all_day,
        video_link=None,
        status="confirmed",
        extended_private=dict(extended_private or {}),
    )


def _plan(
    events: Iterable[CalendarEvent],
    *,
    minutes: dict[str, float] | None = None,
    default_minutes: float = 20,
    min_home_dwell_min: int = 45,
    origin_lookback_min: int = 60,
) -> list[PlannedLeg]:
    """desired_legs -> build_planned_legs with a flat duration for every leg."""
    requests = _requests(events, origin_lookback_min=origin_lookback_min)
    durations = {request.key: default_minutes for request in requests}
    durations.update(minutes or {})
    return build_planned_legs(requests, durations, min_home_dwell_min=min_home_dwell_min)


def _requests(
    events: Iterable[CalendarEvent], *, origin_lookback_min: int = 60
) -> list[LegRequest]:
    return desired_legs(
        {PERSON: list(events)},
        home_address=HOME,
        origin_lookback_min=origin_lookback_min,
        horizon_start=HORIZON_START,
        horizon_end=HORIZON_END,
        train_keywords=TRAIN_KEYWORDS,
    )


def _shape(legs: Iterable[PlannedLeg]) -> list[tuple[str, str, str, str, str]]:
    """A comparable summary of a plan: leg kind, endpoints, and its time box."""
    return [
        (leg.leg, leg.origin, leg.destination, leg.start.isoformat(), leg.end.isoformat())
        for leg in legs
    ]


# --------------------------------------------------------------- marker


def test_travel_block_marker_survives_normalize_event() -> None:
    """The read path must carry `extendedProperties.private` through (#265)."""
    raw: dict[str, Any] = {
        "id": "blk1",
        "summary": "🚗 Trayecto",
        "start": {"dateTime": "2026-07-20T08:40:00+00:00"},
        "end": {"dateTime": "2026-07-20T09:00:00+00:00"},
        "extendedProperties": {"private": {travel_blocks.MARKER_KEY: "1", "wr_leg": "outbound"}},
    }
    event = normalize_event(raw, calendar_id=CALENDAR_ID)
    assert event.extended_private[travel_blocks.MARKER_KEY] == "1"
    assert is_travel_block(event) is True


def test_is_travel_block_only_matches_our_marker() -> None:
    assert is_travel_block(_event(eid="e1")) is False
    # A human typing the block's title is not a block — the marker is the proof.
    assert is_travel_block(_event(eid="e2", summary="🚗 Trayecto")) is False
    assert is_travel_block(_event(eid="e3", extended_private={"someone_else": "1"})) is False
    assert is_travel_block(_event(eid="e4", extended_private={"wr_travel_block": "0"})) is False
    assert is_travel_block(_event(eid="e5", extended_private={"wr_travel_block": "1"})) is True


def test_block_marker_round_trips_through_is_travel_block() -> None:
    """What the writer stamps is exactly what the reader recognizes."""
    leg = _plan([_event(eid="e1")])[0]
    marker = travel_blocks.block_marker(leg)
    assert marker[travel_blocks.SOURCE_EVENT_KEY] == "e1"
    assert marker[travel_blocks.HASH_KEY] == leg.content_hash
    assert is_travel_block(_event(eid="blk", extended_private=marker)) is True


# --------------------------------------------------------------- read-seam guard


class _FakeCalendarClient:
    def __init__(self, raw_by_calendar: dict[str, list[dict[str, Any]]]) -> None:
        self._raw = raw_by_calendar
        self.closed = False

    def list_events(
        self, *, calendar_id: str, time_min: datetime, time_max: datetime
    ) -> list[dict[str, Any]]:
        return self._raw.get(calendar_id, [])

    def close(self) -> None:
        self.closed = True


def _raw_human(eid: str, *, location: str = OFFICE, hour: int = 9) -> dict[str, Any]:
    start = _at(hour)
    return {
        "id": eid,
        "summary": "Appointment",
        "location": location,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
    }


def _raw_block(leg: PlannedLeg) -> dict[str, Any]:
    """The calendar resource step 3 of #263 will write for ``leg`` — marker and all."""
    return {
        "id": f"blk-{leg.key}",
        "summary": "🚗 Trayecto",
        "location": leg.destination,
        "start": {"dateTime": leg.start.isoformat()},
        "end": {"dateTime": leg.end.isoformat()},
        "extendedProperties": {"private": travel_blocks.block_marker(leg)},
    }


def _fetch(monkeypatch: pytest.MonkeyPatch, raw: list[dict[str, Any]]) -> dict[str, list[Any]]:
    client = _FakeCalendarClient({CALENDAR_ID: raw})
    monkeypatch.setattr(calendar_source, "build_google_calendar_client", lambda _p: client)
    return calendar_source.fetch_events_by_person(
        CalendarConfig(accounts=(CalendarAccount(calendar_id=CALENDAR_ID, person=PERSON),)),
        time_min=HORIZON_START,
        time_max=HORIZON_END,
    )


def test_read_seam_drops_travel_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """None of the five consumers can ever see one — the filter is upstream of all."""
    block = _plan([_event(eid="e1")])[0]
    fetched = _fetch(monkeypatch, [_raw_human("e1"), _raw_block(block)])
    assert [event.event_id for event in fetched[PERSON]] == ["e1"]


def test_read_seam_keeps_a_person_key_when_only_blocks_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filtered-to-empty person must not vanish — find_conflicts reads availability."""
    block = _plan([_event(eid="e1")])[0]
    fetched = _fetch(monkeypatch, [_raw_block(block)])
    assert fetched == {PERSON: []}


# --------------------------------------------------------------- desired legs


def test_isolated_event_gets_outbound_and_return() -> None:
    legs = _plan([_event(eid="e1", start=_at(9))])
    assert _shape(legs) == [
        (LEG_OUTBOUND, HOME, OFFICE, _at(8, 40).isoformat(), _at(9).isoformat()),
        (LEG_RETURN, OFFICE, HOME, _at(10).isoformat(), _at(10, 20).isoformat()),
    ]


def test_back_to_back_events_give_one_hop_and_no_phantom_return() -> None:
    """A→B chained: one outbound priced from the previous destination, no trip home."""
    legs = _plan([
        _event(eid="e1", location=OFFICE, start=_at(9)),
        _event(eid="e2", location=CLINIC, start=_at(10, 30)),  # 30 min gap
    ])
    assert _shape(legs) == [
        (LEG_OUTBOUND, HOME, OFFICE, _at(8, 40).isoformat(), _at(9).isoformat()),
        (LEG_OUTBOUND, OFFICE, CLINIC, _at(10, 10).isoformat(), _at(10, 30).isoformat()),
        (LEG_RETURN, CLINIC, HOME, _at(11, 30).isoformat(), _at(11, 50).isoformat()),
    ]


def test_wide_gap_gives_return_home_then_a_fresh_outbound() -> None:
    legs = _plan([
        _event(eid="e1", location=OFFICE, start=_at(9)),
        _event(eid="e2", location=CLINIC, start=_at(13)),  # 180 min gap
    ])
    assert _shape(legs) == [
        (LEG_OUTBOUND, HOME, OFFICE, _at(8, 40).isoformat(), _at(9).isoformat()),
        (LEG_RETURN, OFFICE, HOME, _at(10).isoformat(), _at(10, 20).isoformat()),
        (LEG_OUTBOUND, HOME, CLINIC, _at(12, 40).isoformat(), _at(13).isoformat()),
        (LEG_RETURN, CLINIC, HOME, _at(14).isoformat(), _at(14, 20).isoformat()),
    ]


def test_next_event_at_the_same_address_still_suppresses_the_return() -> None:
    """A→A costs no drive, so there is no outbound to chain onto — but no trip home either."""
    legs = _plan([
        _event(eid="e1", location=OFFICE, start=_at(9)),
        _event(eid="e2", location=OFFICE, start=_at(10, 30)),
    ])
    assert _shape(legs) == [
        (LEG_OUTBOUND, HOME, OFFICE, _at(8, 40).isoformat(), _at(9).isoformat()),
        (LEG_RETURN, OFFICE, HOME, _at(11, 30).isoformat(), _at(11, 50).isoformat()),
    ]


def test_events_outside_the_horizon_produce_no_legs_but_still_chain() -> None:
    """Context beyond the horizon is an origin, never a block of its own."""
    before = _event(eid="e0", location=OFFICE, start=HORIZON_START - timedelta(minutes=90))
    inside = _event(eid="e1", location=CLINIC, start=HORIZON_START + timedelta(minutes=10))
    requests = _requests([before, inside])
    assert {request.source_event_id for request in requests} == {"e1"}
    outbound = next(r for r in requests if r.leg == LEG_OUTBOUND)
    assert outbound.origin == OFFICE  # chained off the out-of-horizon event


# --------------------------------------------------------------- skip cases


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"all_day": True}, "all-day events have no departure moment"),
        ({"summary": "Trabajo (en casa)"}, "explicitly at home"),
        ({"location": "meet.google.com/abc-defg-hij"}, "video-only is not a place"),
        ({"location": ""}, "no location: assumed home is an assumption, not a fact"),
        ({"location": HOME}, "destination is the origin"),
        ({"summary": "Oficina (en tren)"}, "a DRIVE duration is meaningless for a train"),
        ({"extended_private": {"wr_travel_block": "1"}}, "one of our own blocks"),
    ],
)
def test_skip_cases_emit_no_leg(kwargs: dict[str, Any], why: str) -> None:
    assert _requests([_event(eid="e1", **kwargs)]) == [], why


def test_blank_home_address_plans_nothing() -> None:
    """The committed default ships a blank home address — never plan a drive to nowhere."""
    assert (
        desired_legs(
            {PERSON: [_event(eid="e1")]},
            home_address="  ",
            origin_lookback_min=60,
            horizon_start=HORIZON_START,
            horizon_end=HORIZON_END,
            train_keywords=TRAIN_KEYWORDS,
        )
        == []
    )


# --------------------------------------------------------------- dwell threshold


def test_chains_directly_on_both_sides_of_the_boundary() -> None:
    # drive_home 20 + drive_out 15 + dwell 45 => 80 minutes of gap needed.
    assert chains_directly(79, 20, 15, 45) is True  # just under => direct hop
    assert chains_directly(80, 20, 15, 45) is False  # exactly enough => go home
    assert chains_directly(81, 20, 15, 45) is False  # just over => go home


def test_dwell_threshold_decides_the_return_block() -> None:
    """The same two events flip the return block purely on min_home_dwell_min."""
    events = [
        _event(eid="e1", location=OFFICE, start=_at(9)),
        _event(eid="e2", location=CLINIC, start=_at(11)),  # 60 min gap
    ]
    # 60 < 20 + 20 + 45 => chained, no return after e1.
    chained = _plan(events, min_home_dwell_min=45)
    assert [leg.leg for leg in chained] == [LEG_OUTBOUND, LEG_OUTBOUND, LEG_RETURN]
    # 60 >= 20 + 20 + 10 => worth going home.
    goes_home = _plan(events, min_home_dwell_min=10)
    assert [leg.leg for leg in goes_home] == [
        LEG_OUTBOUND,
        LEG_RETURN,
        LEG_OUTBOUND,
        LEG_RETURN,
    ]


# --------------------------------------------------------------- durations


def test_unpriced_leg_is_dropped_never_zero_length() -> None:
    requests = _requests([_event(eid="e1")])
    outbound = next(r for r in requests if r.leg == LEG_OUTBOUND)
    legs = build_planned_legs(requests, {outbound.key: 20}, min_home_dwell_min=45)
    assert [leg.leg for leg in legs] == [LEG_OUTBOUND]


def test_non_positive_duration_is_dropped() -> None:
    requests = _requests([_event(eid="e1")])
    legs = build_planned_legs(
        requests, {request.key: 0 for request in requests}, min_home_dwell_min=45
    )
    assert legs == []


def test_unpriced_next_outbound_keeps_the_return_home() -> None:
    """Dropping both would leave the drive unrepresented entirely."""
    requests = _requests([
        _event(eid="e1", location=OFFICE, start=_at(9)),
        _event(eid="e2", location=CLINIC, start=_at(10, 30)),
    ])
    priced = {
        request.key: 20 for request in requests if request.source_event_id == "e1"
    }
    legs = build_planned_legs(requests, priced, min_home_dwell_min=45)
    assert [(leg.leg, leg.source_event_id) for leg in legs] == [
        (LEG_OUTBOUND, "e1"),
        (LEG_RETURN, "e1"),
    ]


def test_leg_request_anchor_is_arrival_for_outbound_and_departure_for_return() -> None:
    """#266 prices each leg for *its own* moment — traffic depends on the clock."""
    requests = _requests([_event(eid="e1", start=_at(9))])
    anchors = {request.leg: request.anchor for request in requests}
    assert anchors[LEG_OUTBOUND] == _at(9)  # arrive by
    assert anchors[LEG_RETURN] == _at(10)  # depart at


# --------------------------------------------------------------- content hash


def _hash_inputs() -> dict[str, Any]:
    return {
        "origin": HOME,
        "destination": OFFICE,
        "source_start": _at(9),
        "source_end": _at(10),
        "minutes": 20,
    }


def test_content_hash_is_stable_for_identical_inputs() -> None:
    assert content_hash(**_hash_inputs()) == content_hash(**_hash_inputs())


@pytest.mark.parametrize(
    "change",
    [
        {"origin": CLINIC},
        {"destination": CLINIC},
        {"source_start": _at(9, 30)},
        {"source_end": _at(10, 30)},
        {"minutes": 21},
    ],
)
def test_content_hash_changes_when_any_input_changes(change: dict[str, Any]) -> None:
    assert content_hash(**{**_hash_inputs(), **change}) != content_hash(**_hash_inputs())


# --------------------------------------------------------------- feedback loop


def test_two_consecutive_sweeps_produce_an_identical_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The correctness trap of #263: the sweep must never feed on its own output.

    Pass 1 plans over a fixture day. Its blocks are then written back into the
    calendar the second pass reads — through the *real* read seam, marker and
    all — and pass 2 must produce byte-identical legs and not one extra.
    """
    human = [_raw_human("e1", location=OFFICE, hour=9), _raw_human("e2", location=CLINIC, hour=13)]

    first_events = _fetch(monkeypatch, human)
    first_plan = _plan(first_events[PERSON])
    assert first_plan, "fixture must actually produce blocks or this proves nothing"

    written = human + [_raw_block(leg) for leg in first_plan]
    second_events = _fetch(monkeypatch, written)
    second_plan = _plan(second_events[PERSON])

    assert _shape(second_plan) == _shape(first_plan)
    assert len(second_plan) == len(first_plan)
    assert [leg.content_hash for leg in second_plan] == [leg.content_hash for leg in first_plan]


def test_planner_ignores_travel_blocks_even_without_the_read_seam() -> None:
    """Belt-and-braces: a caller that skips the seam still cannot loop the planner."""
    human = _event(eid="e1", location=OFFICE, start=_at(9))
    baseline = _plan([human])
    blocks = [
        _event(
            eid=f"blk-{leg.key}",
            location=leg.destination,
            summary="🚗 Trayecto",
            start=leg.start,
            end=leg.end,
            extended_private=travel_blocks.block_marker(leg),
        )
        for leg in baseline
    ]
    assert _shape(_plan([human, *blocks])) == _shape(baseline)
