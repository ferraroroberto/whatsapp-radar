"""Unit tests for the deterministic family decision core (issue #160).

Pure, offline — no network, no calendar client. Exercises the classification,
origin-chaining, dedup, quiet-hours, and conflict rules against the spec.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from calendar_readonly.core import CalendarEvent

from src.config import ChildcareWindow, FamilyConfig
from src.family import rules

HOME = "Carrer Example 30, Sant Cugat"
WORK = "Avenida Diagonal 621, Barcelona"


def _event(
    summary: str = "",
    *,
    location: str = "",
    video_link: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    all_day: bool = False,
    eid: str = "e1",
    calendar_id: str = "roberto@x",
) -> CalendarEvent:
    start = start or datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    end = end or (start + timedelta(hours=1))
    return CalendarEvent(
        event_id=eid,
        calendar_id=calendar_id,
        summary=summary,
        location=location,
        description="",
        start=start,
        end=end,
        all_day=all_day,
        video_link=video_link,
        status="confirmed",
    )


# --------------------------------------------------------------- addresses


def test_same_address_loose_match():
    assert rules.same_address("Av. Diagonal 621", "av diagonal 621, barcelona")
    assert not rules.same_address("Diagonal 621", "Provençals 33")
    assert not rules.same_address("", "anything")


def test_physical_location_ignores_video_only():
    assert rules.physical_location(_event(location="meet.google.com/abc")) is None
    assert rules.physical_location(_event(location=WORK)) == WORK
    # Hybrid: a video hint plus real address text is still physical.
    assert rules.physical_location(_event(location="meet.google.com/x also " + WORK)) is not None


# --------------------------------------------------------------- commute


def test_requires_commute_rules():
    assert rules.requires_commute(_event("Dentist", location=WORK), HOME)
    assert not rules.requires_commute(_event("Standup (en casa)", location=WORK), HOME)
    assert not rules.requires_commute(_event("Sync", video_link="meet"), HOME)
    assert not rules.requires_commute(_event("At home", location=HOME), HOME)
    # Hybrid appointment (video + physical) still requires driving there.
    assert rules.requires_commute(_event("Clinic", location="zoom.us/j and " + WORK), HOME)


def test_decide_location_table():
    # Table-driven: (event, expected kind, expected source, expected assumed).
    cases = [
        (_event("Call", video_link="meet"), "home", "video_only", False),
        (_event("Reunión (en casa)"), "home", "en_casa_marker", False),
        (_event("Meeting", location=WORK), "away", "physical_address", False),
        (_event("Home thing", location=HOME), "home", "home_address", False),
        # Semantics flip (#168): no location at all is assumed home — visibly.
        (_event("Mystery"), "home", "assumed_home", True),
    ]
    for event, kind, source, assumed in cases:
        decision = rules.decide_location(event, HOME)
        assert (decision.kind, decision.source, decision.assumed) == (kind, source, assumed), (
            event.summary
        )
        assert rules.location_kind(event, HOME) == kind


# --------------------------------------------------------------- origin


def test_resolve_origin_defaults_home():
    ev = _event("Office", location=WORK, start=datetime(2026, 7, 20, 9, 0, tzinfo=UTC))
    assert rules.resolve_origin(ev, [ev], home_address=HOME, lookback_min=60) == HOME


def test_resolve_origin_chains_back_to_back():
    lunch_spot = "Carrer Provençals 33, Barcelona"
    first = _event("Office", location=WORK, eid="a",
                   start=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
                   end=datetime(2026, 7, 20, 12, 30, tzinfo=UTC))
    lunch = _event("Lunch meeting", location=lunch_spot, eid="b",
                   start=datetime(2026, 7, 20, 13, 0, tzinfo=UTC),
                   end=datetime(2026, 7, 20, 14, 0, tzinfo=UTC))
    origin = rules.resolve_origin(lunch, [first, lunch], home_address=HOME, lookback_min=60)
    assert origin == WORK  # chained from the office, not home


def test_resolve_origin_ignores_stale_prior():
    first = _event("Office", location=WORK, eid="a",
                   start=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
                   end=datetime(2026, 7, 20, 10, 0, tzinfo=UTC))
    later = _event("Afternoon", location="Somewhere else 5", eid="b",
                   start=datetime(2026, 7, 20, 15, 0, tzinfo=UTC),
                   end=datetime(2026, 7, 20, 16, 0, tzinfo=UTC))
    # 5h gap ⇒ prior office event is outside the 60-min lookback ⇒ origin is home.
    assert rules.resolve_origin(later, [first, later], home_address=HOME, lookback_min=60) == HOME


def test_upcoming_commutes_filters():
    now = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    commute = _event("Office", location=WORK, eid="a",
                     start=datetime(2026, 7, 20, 9, 0, tzinfo=UTC))
    allday = _event("Holiday", location=WORK, eid="b", all_day=True,
                    start=datetime(2026, 7, 20, 0, 0, tzinfo=UTC))
    virtual = _event("Call", video_link="meet", eid="c",
                     start=datetime(2026, 7, 20, 10, 0, tzinfo=UTC))
    legs = rules.upcoming_commutes(
        {"roberto": [commute, allday, virtual]},
        home_address=HOME, now=now, lookahead=timedelta(hours=6), origin_lookback_min=60,
    )
    assert [leg.event.event_id for leg in legs] == ["a"]
    assert legs[0].origin == HOME and legs[0].destination == WORK
    assert legs[0].origin_event_end is None  # from home ⇒ no chained departure


def test_upcoming_commutes_records_chained_origin_end():
    now = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    office = _event("Office", location=WORK, eid="a",
                    start=datetime(2026, 7, 20, 8, 30, tzinfo=UTC),
                    end=datetime(2026, 7, 20, 9, 0, tzinfo=UTC))
    lunch = _event("Lunch", location="Carrer de la Marina 16, Barcelona", eid="b",
                   start=datetime(2026, 7, 20, 9, 10, tzinfo=UTC),
                   end=datetime(2026, 7, 20, 10, 0, tzinfo=UTC))
    legs = rules.upcoming_commutes(
        {"roberto": [office, lunch]},
        home_address=HOME, now=now, lookahead=timedelta(hours=6), origin_lookback_min=60,
    )
    lunch_leg = next(leg for leg in legs if leg.event.event_id == "b")
    # Chained off the office ⇒ origin is the office and the gap anchor is its end.
    assert lunch_leg.origin == WORK
    assert lunch_leg.origin_event_end == office.end


# --------------------------------------------------------------- dedup + quiet


def test_dedup_key_stable():
    assert rules.dedup_key("roberto", "  Office  RUN ") == rules.dedup_key("roberto", "office run")


def test_in_quiet_hours_wraps_midnight():
    assert rules.in_quiet_hours(datetime(2026, 7, 20, 22, 0, tzinfo=UTC), 20, 5)
    assert rules.in_quiet_hours(datetime(2026, 7, 20, 3, 0, tzinfo=UTC), 20, 5)
    assert not rules.in_quiet_hours(datetime(2026, 7, 20, 12, 0, tzinfo=UTC), 20, 5)
    assert not rules.in_quiet_hours(datetime(2026, 7, 20, 12, 0, tzinfo=UTC), 5, 5)


# --------------------------------------------------------------- conflicts


def _family() -> FamilyConfig:
    return FamilyConfig(
        enabled=True,
        home_address=HOME,
        kids_home_time="17:30",
        responsible_by_weekday={0: "roberto", 1: "ana", 2: "ana", 3: "roberto", 4: "ana"},
        childcare_windows=(
            ChildcareWindow(label="swimming", weekdays=(0, 2, 4), time="16:45"),
        ),
    )


def test_find_conflicts_flags_responsible_parent_away():
    # Monday 2026-07-20; Roberto is on duty. Roberto away over the 16:45 swim.
    day = datetime(2026, 7, 20).date()
    roberto_away = _event("Meeting", location=WORK, calendar_id="roberto@x",
                          start=datetime(2026, 7, 20, 16, 0, tzinfo=UTC),
                          end=datetime(2026, 7, 20, 18, 0, tzinfo=UTC))
    conflicts = rules.find_conflicts(
        {"roberto": [roberto_away], "ana": []}, _family(), day=day, tz=UTC
    )
    assert any(c.kind == "coverage_gap" and "swimming" in c.detail for c in conflicts)


def test_find_conflicts_quiet_when_responsible_home():
    day = datetime(2026, 7, 20).date()  # Monday
    home_ev = _event("Reunión (en casa)", calendar_id="roberto@x",
                     start=datetime(2026, 7, 20, 16, 0, tzinfo=UTC),
                     end=datetime(2026, 7, 20, 18, 0, tzinfo=UTC))
    conflicts = rules.find_conflicts(
        {"roberto": [home_ev], "ana": []}, _family(), day=day, tz=UTC
    )
    assert conflicts == []


def _family_ranged() -> FamilyConfig:
    """A genuine start-end childcare window (#167), not a point deadline."""
    return FamilyConfig(
        enabled=True,
        home_address=HOME,
        kids_home_time="17:30",
        responsible_by_weekday={5: "roberto"},  # Saturday
        childcare_windows=(
            ChildcareWindow(label="camp", weekdays=(5,), time="09:00", end_time="12:00"),
        ),
    )


def test_find_conflicts_range_window_flags_away_overlapping_end():
    # Saturday; the away commitment only overlaps the tail of the 09:00-12:00
    # window, never the 09:00 start instant — proves the check is range-aware,
    # not just a single-moment containment test.
    day = datetime(2026, 7, 25).date()
    away = _event("Errand", location=WORK, calendar_id="roberto@x",
                  start=datetime(2026, 7, 25, 11, 30, tzinfo=UTC),
                  end=datetime(2026, 7, 25, 13, 0, tzinfo=UTC))
    conflicts = rules.find_conflicts(
        {"roberto": [away]}, _family_ranged(), day=day, tz=UTC
    )
    assert any(c.kind == "coverage_gap" and "camp" in c.detail for c in conflicts)


def test_find_conflicts_range_window_ignores_commitment_after_window():
    day = datetime(2026, 7, 25).date()
    away = _event("Errand", location=WORK, calendar_id="roberto@x",
                  start=datetime(2026, 7, 25, 12, 30, tzinfo=UTC),
                  end=datetime(2026, 7, 25, 14, 0, tzinfo=UTC))
    conflicts = rules.find_conflicts(
        {"roberto": [away]}, _family_ranged(), day=day, tz=UTC
    )
    assert conflicts == []


def test_find_missing_locations():
    missing = _event("Mystery appointment")  # no location, no video
    known = _event("Office", location=WORK)
    out = rules.find_missing_locations({"roberto": [missing, known]}, home_address=HOME)
    assert [e.summary for _, e in out] == ["Mystery appointment"]
    # All-day events are never asked about — no time to be somewhere at.
    allday = _event("Holiday", all_day=True)
    assert rules.find_missing_locations({"ana": [allday]}, home_address=HOME) == []


OTHER = "Carrer de la Marina 16, Barcelona"


def test_find_overlaps_two_places_at_once():
    t0 = datetime(2026, 7, 20, 17, 0, tzinfo=UTC)
    a = _event("Dentist", location=WORK, start=t0, end=t0 + timedelta(hours=1), eid="a")
    b = _event("Recital", location=OTHER, start=t0 + timedelta(minutes=30),
               end=t0 + timedelta(hours=2), eid="b")
    out = rules.find_overlaps({"ana": [a, b]}, home_address=HOME)
    assert len(out) == 1
    assert out[0].kind == "impossible_overlap"
    assert "'Dentist' and 'Recital'" in out[0].detail


def test_find_overlaps_table_of_non_conflicts():
    t0 = datetime(2026, 7, 20, 17, 0, tzinfo=UTC)
    one_hour = timedelta(hours=1)
    # Table-driven: pairs that must NOT be flagged, and why.
    cases = [
        # Back-to-back adjacency at different addresses: travel feasibility
        # needs live routing (#169) — not judged deterministically here.
        (_event("A", location=WORK, start=t0, end=t0 + one_hour, eid="a"),
         _event("B", location=OTHER, start=t0 + one_hour, end=t0 + 2 * one_hour, eid="b")),
        # Same address overlapping: one place, physically possible.
        (_event("A", location=WORK, start=t0, end=t0 + one_hour, eid="a"),
         _event("B", location=WORK, start=t0, end=t0 + one_hour, eid="b")),
        # Assumed-home vs away overlapping: the assumption is not proof.
        (_event("A", start=t0, end=t0 + one_hour, eid="a"),
         _event("B", location=WORK, start=t0, end=t0 + one_hour, eid="b")),
        # Different people are allowed to be in different places.
    ]
    for a, b in cases:
        assert rules.find_overlaps({"ana": [a, b]}, home_address=HOME) == [], (
            a.summary, b.summary
        )
    x = _event("A", location=WORK, start=t0, end=t0 + one_hour, eid="a")
    y = _event("B", location=OTHER, start=t0, end=t0 + one_hour, eid="b")
    assert rules.find_overlaps({"ana": [x], "roberto": [y]}, home_address=HOME) == []


def test_event_decisions_trace_every_event():
    t0 = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    events = {
        "roberto": [
            _event("Office", location=WORK, start=t0, eid="a"),
            _event("Mystery", start=t0 + timedelta(hours=2), eid="b"),
            _event("Holiday", all_day=True, start=t0, eid="c"),
        ]
    }
    records = rules.event_decisions(events, home_address=HOME)
    assert len(records) == 3  # every event in the window is traced
    by_event = {r["event"]: r for r in records}
    assert by_event["Office"]["kind"] == "away"
    assert by_event["Office"]["source"] == "physical_address"
    assert by_event["Office"]["commute"] is True
    assert by_event["Mystery"]["assumed"] is True
    assert by_event["Mystery"]["source"] == "assumed_home"
    assert by_event["Holiday"]["kind"] == "all_day"
    assert by_event["Holiday"]["source"] == "not_assessed"


def test_day_windows_table():
    """(#177) shared window assembly: weekday filter, range fallback, kids-home."""
    from datetime import date, time

    family = FamilyConfig(
        enabled=True,
        home_address="Carrer Example 30",
        kids_home_time="17:30",
        responsible_by_weekday={0: "roberto"},
        childcare_windows=(
            ChildcareWindow(label="school run", weekdays=(0,), time="08:30"),
            ChildcareWindow(label="afternoon", weekdays=(0,), time="15:00", end_time="18:00"),
            ChildcareWindow(label="inverted", weekdays=(0,), time="16:00", end_time="09:00"),
            ChildcareWindow(label="not monday", weekdays=(2,), time="10:00"),
        ),
    )
    monday = date(2026, 7, 20)
    windows = rules.day_windows(family, monday)
    assert ("school run", time(8, 30), time(8, 30)) in windows
    assert ("afternoon", time(15, 0), time(18, 0)) in windows
    assert ("inverted", time(16, 0), time(16, 0)) in windows  # bad end → point
    assert all(label != "not monday" for label, *_ in windows)
    assert ("kids home", time(17, 30), time(17, 30)) in windows
    # Saturday: no kids-home moment.
    saturday = date(2026, 7, 25)
    assert all(label != "kids home" for label, *_ in rules.day_windows(family, saturday))


def test_arrival_margin_min_floors_toward_late():
    """(#177) a 30-second shortfall already reads as late — never rounded fine."""
    now = datetime(2026, 7, 20, 16, 0, tzinfo=UTC)
    start = now + timedelta(minutes=90)
    assert rules.arrival_margin_min(now, 20.0, start) == 70
    assert rules.arrival_margin_min(now, 90.0, start) == 0    # exactly on time
    assert rules.arrival_margin_min(now, 90.5, start) == -1   # 30 s short ⇒ late
    assert rules.arrival_margin_min(now, 120.0, start) == -30
