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


def test_location_kind():
    assert rules.location_kind(_event("Call", video_link="meet"), HOME) == "home"
    assert rules.location_kind(_event("Reunión (en casa)"), HOME) == "home"
    assert rules.location_kind(_event("Meeting", location=WORK), HOME) == "away"
    assert rules.location_kind(_event("Home thing", location=HOME), HOME) == "home"
    # No location at all is Unknown, never home.
    assert rules.location_kind(_event("Mystery"), HOME) == "unknown"


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


def test_find_unknown_locations():
    unknown = _event("Mystery appointment")  # no location, no video
    known = _event("Office", location=WORK)
    out = rules.find_unknown_locations({"roberto": [unknown, known]}, home_address=HOME)
    assert [e.summary for _, e in out] == ["Mystery appointment"]
