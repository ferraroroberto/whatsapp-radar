"""Traffic-check presence integration (#169): live origin, fallback, privacy.

Offline — the calendar fetch, the presence lookup, the Routes call, and the
alert sender are all stubbed; sanitized fixture events only. Covers the three
origin outcomes (fresh phone fix used, unavailable → calendar-inference
fallback), the decision-trace source field, back-to-back adjacency feasibility
(completing #168's deferral), and the hard privacy rule that no raw coordinates
ever land in the persisted payload.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from calendar_readonly.core import CalendarEvent

from src.config import (
    Config,
    FamilyConfig,
    HubConfig,
    PresenceConfig,
    TelegramConfig,
    TrafficConfig,
)
from src.family import traffic_check
from src.presence import PresenceLocation, PresenceUnavailable
from src.traffic import RouteResult

HOME = "Carrer Example 30, Sant Cugat"
WORK = "Avenida Diagonal 621, Barcelona"
LUNCH = "Carrer de la Marina 16, Barcelona"
LAT, LON = 41.55512, 2.34567  # placeholder coords, never a real location

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)  # daytime, outside quiet hours


def _event(
    summary: str, *, location: str, start: datetime, end: datetime, eid: str
) -> CalendarEvent:
    return CalendarEvent(
        event_id=eid, calendar_id="roberto@x", summary=summary, location=location,
        description="", start=start, end=end, all_day=False, video_link=None,
        status="confirmed",
    )


def _config(*, presence_enabled: bool = True) -> Config:
    return Config(
        db_path="unused.sqlite3",  # type: ignore[arg-type]
        connector="fixture", classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="m"),
        notifier="telegram", telegram=TelegramConfig(bot_token="t", chat_id="c"),
        linked_device_dir="ld",  # type: ignore[arg-type]
        traffic=TrafficConfig(enabled=True, api_key="k", significant_delay_min=15),
        family=FamilyConfig(enabled=True, home_address=HOME),
        presence=PresenceConfig(enabled=presence_enabled),
    )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the four seams; capture the compute_route calls and sent alerts."""
    state: dict[str, Any] = {"route_calls": [], "sent": [], "events": {}, "route": None}

    def fake_fetch(*a: Any, **kw: Any) -> dict[str, list[CalendarEvent]]:
        return state["events"]

    def fake_route(origin: str, destination: str, **kw: Any) -> RouteResult:
        state["route_calls"].append({"origin": origin, "destination": destination, **kw})
        return state["route"] or RouteResult(normal_s=600, traffic_s=600)

    def fake_send(config: Config, text: str) -> tuple[str, str | None]:
        state["sent"].append(text)
        return "sent", None

    monkeypatch.setattr(traffic_check, "fetch_events_by_person", fake_fetch)
    monkeypatch.setattr(traffic_check, "compute_route", fake_route)
    monkeypatch.setattr(traffic_check, "send_alert", fake_send)
    monkeypatch.setattr(traffic_check.dedup, "recent_keys", lambda *a, **kw: set())
    monkeypatch.setattr(traffic_check.dedup, "record_alert", lambda *a, **kw: None)
    return state


def _fresh_location(person: str = "roberto") -> PresenceLocation:
    return PresenceLocation(
        person=person, latitude=LAT, longitude=LON, at_home=False,
        distance_from_home_km=3.2, age_min=2.0, refreshed=False,
    )


def _single_office_leg(state: dict[str, Any]) -> None:
    state["events"] = {
        "roberto": [
            _event("Office", location=WORK,
                   start=NOW + timedelta(minutes=30), end=NOW + timedelta(hours=2), eid="a")
        ]
    }


def test_live_presence_origin_is_used(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _single_office_leg(harness)
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=True)

    entry = payload["checked"][0]
    assert entry["location_source"] == "live_presence"
    assert entry["origin"] == "live phone position"
    assert entry["presence_age_min"] == 2.0
    # The Routes call routed from the exact fix, not an address string.
    assert harness["route_calls"][0]["origin_latlng"] == (LAT, LON)


def test_fallback_to_calendar_inference_when_unavailable(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _single_office_leg(harness)
    monkeypatch.setattr(
        traffic_check, "get_location",
        lambda *a, **kw: PresenceUnavailable("roberto", "transport_error"),
    )
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=True)

    entry = payload["checked"][0]
    assert entry["location_source"] == "calendar_inference"
    assert entry["presence_status"] == "transport_error"
    assert entry["origin"] == HOME  # calendar-inference origin (from home)
    assert harness["route_calls"][0]["origin_latlng"] is None


def test_payload_carries_no_raw_coordinates(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _single_office_leg(harness)
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=True)

    blob = json.dumps(payload)
    assert "latitude" not in blob and "longitude" not in blob
    assert str(LAT) not in blob and str(LON) not in blob


def test_back_to_back_infeasible_is_flagged_and_alerts(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Office ends 09:00; Lunch (different place) starts 09:10 — a 10-min gap, but
    # the drive takes 25 min in traffic, so the hop is infeasible.
    harness["events"] = {
        "roberto": [
            _event("Office", location=WORK,
                   start=NOW - timedelta(hours=1), end=NOW, eid="a"),
            _event("Lunch", location=LUNCH,
                   start=NOW + timedelta(minutes=10), end=NOW + timedelta(hours=1), eid="b"),
        ]
    }
    harness["route"] = RouteResult(normal_s=1500, traffic_s=1500)  # 25 min, no "delay"
    # Presence off so the calendar-chained origin (and its gap) is what's judged.
    monkeypatch.setattr(
        traffic_check, "get_location",
        lambda *a, **kw: PresenceUnavailable("roberto", "disabled"),
    )
    payload = traffic_check.run_traffic_check(
        _config(presence_enabled=False), now=NOW, dry_run=False
    )

    entry = next(e for e in payload["checked"] if e["event"] == "Lunch")
    assert entry["location_source"] == "calendar_inference"
    assert entry["origin"] == WORK and entry["gap_min"] == 10
    assert entry["feasible"] is False
    assert entry["alerted"] is True and payload["alerts"] == 1
    assert "Tight schedule" in harness["sent"][0]


def test_feasible_leg_does_not_alert(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    harness["events"] = {
        "roberto": [
            _event("Office", location=WORK,
                   start=NOW - timedelta(hours=1), end=NOW, eid="a"),
            _event("Lunch", location=LUNCH,
                   start=NOW + timedelta(minutes=40), end=NOW + timedelta(hours=1), eid="b"),
        ]
    }
    harness["route"] = RouteResult(normal_s=600, traffic_s=600)  # 10 min drive, 40 min gap
    monkeypatch.setattr(
        traffic_check, "get_location",
        lambda *a, **kw: PresenceUnavailable("roberto", "disabled"),
    )
    payload = traffic_check.run_traffic_check(
        _config(presence_enabled=False), now=NOW, dry_run=False
    )
    entry = next(e for e in payload["checked"] if e["event"] == "Lunch")
    assert entry["feasible"] is True
    assert entry["alerted"] is False and harness["sent"] == []


def test_live_presence_origin_is_not_feasibility_judged(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A live fix has no fixed departure moment, so a chained gap must not apply.
    harness["events"] = {
        "roberto": [
            _event("Office", location=WORK,
                   start=NOW - timedelta(hours=1), end=NOW, eid="a"),
            _event("Lunch", location=LUNCH,
                   start=NOW + timedelta(minutes=10), end=NOW + timedelta(hours=1), eid="b"),
        ]
    }
    harness["route"] = RouteResult(normal_s=1500, traffic_s=1500)
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=True)
    entry = next(e for e in payload["checked"] if e["event"] == "Lunch")
    assert entry["location_source"] == "live_presence"
    assert entry["feasible"] is None and entry["gap_min"] is None


def test_leave_now_alerts_on_live_fix_at_departure_moment(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Office starts in 30 min; the live drive is 50 min and the margin is 5, so
    # depart_in = 30 - (50 + 5) = -25 ⇒ leave now (they are already overdue).
    _single_office_leg(harness)
    harness["route"] = RouteResult(normal_s=3000, traffic_s=3000)  # 50 min, no delay
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    entry = payload["checked"][0]
    assert entry["location_source"] == "live_presence"
    assert entry["depart_in_min"] == -25 and entry["leave_margin_min"] == 5
    assert entry["leave_now_alerted"] is True and payload["alerts"] == 1
    assert "Leave now" in harness["sent"][0]
    assert entry["alerted"] is False  # 0-min delay, so no separate delay alert


def test_leave_now_silent_when_departure_not_yet_due(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Office starts in 30 min; a 10-min drive + 5-min margin leaves 15 min of
    # slack ⇒ depart_in = 15 > 0, no leave-now alert yet.
    _single_office_leg(harness)
    harness["route"] = RouteResult(normal_s=600, traffic_s=600)  # 10 min
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    entry = payload["checked"][0]
    assert entry["depart_in_min"] == 15
    assert entry["leave_now_alerted"] is False and harness["sent"] == []


def test_leave_now_never_on_calendar_inference_origin(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # No live fix: even though the drive dwarfs the slack, a calendar-inference
    # origin makes no claim about where the person is, so no leave-now alert.
    _single_office_leg(harness)
    harness["route"] = RouteResult(normal_s=1200, traffic_s=1200)  # 20 min
    monkeypatch.setattr(
        traffic_check, "get_location",
        lambda *a, **kw: PresenceUnavailable("roberto", "disabled"),
    )
    payload = traffic_check.run_traffic_check(
        _config(presence_enabled=False), now=NOW, dry_run=False
    )
    entry = payload["checked"][0]
    assert entry["location_source"] == "calendar_inference"
    assert entry["depart_in_min"] is None
    assert entry["leave_now_alerted"] is False and harness["sent"] == []


def test_leave_now_deduped_independently_of_delay_alert(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A significant delay AND an overdue departure fire together for one event,
    # each under its own dedup key; a prior leave-now key does not suppress the
    # delay alert, and vice versa.
    _single_office_leg(harness)
    harness["route"] = RouteResult(normal_s=600, traffic_s=2400)  # 40 min, +30 delay
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())

    recorded: list[str] = []
    monkeypatch.setattr(traffic_check.dedup, "record_alert",
                        lambda key, **kw: recorded.append(key))
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    entry = payload["checked"][0]
    assert entry["alerted"] is True and entry["leave_now_alerted"] is True
    assert payload["alerts"] == 2 and len(harness["sent"]) == 2
    assert any("Traffic alert" in t for t in harness["sent"])
    assert any("Leave now" in t for t in harness["sent"])
    # Two distinct dedup keys recorded — the plain key and the ::leave-now key.
    assert len(recorded) == 2 and recorded[1].endswith("::leave-now")


def test_leave_now_suppressed_when_key_already_recent(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.family import rules

    _single_office_leg(harness)
    harness["route"] = RouteResult(normal_s=3000, traffic_s=3000)  # 50 min ⇒ overdue
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    leave_key = rules.leave_now_dedup_key("roberto", "Office")
    monkeypatch.setattr(traffic_check.dedup, "recent_keys", lambda *a, **kw: {leave_key})
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    entry = payload["checked"][0]
    assert entry["leave_now_alerted"] is False and harness["sent"] == []


def test_disabled_check_is_silent(harness: dict[str, Any]) -> None:
    disabled = dataclasses.replace(_config(), traffic=TrafficConfig(enabled=False, api_key="k"))
    payload = traffic_check.run_traffic_check(disabled, now=NOW, dry_run=False)
    assert payload["status"] == "disabled"
    assert harness["sent"] == []
