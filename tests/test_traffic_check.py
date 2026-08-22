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
    # Office ends 09:20; Lunch (different place) starts 09:30 — a 10-min gap, but
    # the drive takes 25 min in traffic, so the hop is infeasible. Office is still
    # running at `now`, so the hop's departure anchor (#270) is in the future and
    # the leg is actually priced.
    harness["events"] = {
        "roberto": [
            _event("Office", location=WORK,
                   start=NOW - timedelta(hours=1), end=NOW + timedelta(minutes=20), eid="a"),
            _event("Lunch", location=LUNCH,
                   start=NOW + timedelta(minutes=30), end=NOW + timedelta(hours=1), eid="b"),
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
    # The hop is priced for the moment the person sets off — the end of the
    # event they are leaving, the same instant `gap_min` is measured from (#270).
    assert entry["anchor"] == "preceding_event_end"
    assert harness["route_calls"][0]["departure_time"] == NOW + timedelta(minutes=20)
    assert entry["feasible"] is False
    assert entry["alerted"] is True and payload["alerts"] == 1
    assert "Tight schedule" in harness["sent"][0]


def test_feasible_leg_does_not_alert(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    harness["events"] = {
        "roberto": [
            _event("Office", location=WORK,
                   start=NOW - timedelta(hours=1), end=NOW + timedelta(minutes=20), eid="a"),
            _event("Lunch", location=LUNCH,
                   start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2), eid="b"),
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


def test_leave_now_merges_when_two_people_share_identical_event_text(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both roberto and ana are live-tracked, overdue for the same shared event,
    # with the same resulting text (#213) — one combined Telegram message, not
    # two redundant ones.
    harness["events"] = {
        "roberto": [
            _event("Movento", location=WORK,
                   start=NOW + timedelta(minutes=30), end=NOW + timedelta(hours=2), eid="a")
        ],
        "ana": [
            _event("Movento", location=WORK,
                   start=NOW + timedelta(minutes=30), end=NOW + timedelta(hours=2), eid="b")
        ],
    }
    harness["route"] = RouteResult(normal_s=3000, traffic_s=3000)  # 50 min, no delay
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    recorded: list[str] = []
    monkeypatch.setattr(traffic_check.dedup, "record_alert",
                        lambda key, **kw: recorded.append(key))
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    assert harness["sent"] == ['🚗 Leave now — roberto and ana: “Movento”. '
                                'Drive is ~50 min with traffic; it starts at 09:30.']
    assert payload["alerts"] == 1
    entries = [e for e in payload["checked"] if e["leave_now_alerted"]]
    assert len(entries) == 2  # both legs marked alerted from the one send
    # Each person's own dedup key is still recorded so a later run doesn't
    # re-fire for either of them individually.
    assert sorted(recorded) == sorted([
        "ana::movento::leave-now", "roberto::movento::leave-now",
    ])


def test_leave_now_not_merged_when_event_start_differs(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same event name and live drive time, but different start times ⇒ the
    # resulting text actually differs ("starts at HH:MM"), so no merge.
    harness["events"] = {
        "roberto": [
            _event("Movento", location=WORK,
                   start=NOW + timedelta(minutes=30), end=NOW + timedelta(hours=2), eid="a")
        ],
        "ana": [
            _event("Movento", location=WORK,
                   start=NOW + timedelta(minutes=40), end=NOW + timedelta(hours=2), eid="b")
        ],
    }
    harness["route"] = RouteResult(normal_s=3000, traffic_s=3000)  # 50 min, both overdue
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    assert len(harness["sent"]) == 2 and payload["alerts"] == 2
    assert not any(" and " in t for t in harness["sent"])
    assert any("roberto" in t for t in harness["sent"])
    assert any("ana" in t for t in harness["sent"])


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


def _train_office_leg(state: dict[str, Any]) -> None:
    """The daily office run, titled as taken by train (#227)."""
    state["events"] = {
        "roberto": [
            _event("Trabajo desde la oficina (en tren)", location=WORK,
                   start=NOW + timedelta(minutes=30), end=NOW + timedelta(hours=2), eid="a")
        ]
    }


def test_leave_now_skipped_for_train_titled_commute(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same overdue-departure setup as the leave-now test above, but the event is
    # a train ride: the driving ETA says nothing about it, so no nudge fires and
    # the suppression is recorded in the trace rather than silently dropped.
    _train_office_leg(harness)
    harness["route"] = RouteResult(normal_s=3000, traffic_s=3000)  # 50 min ⇒ overdue
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    entry = payload["checked"][0]
    assert entry["depart_in_min"] == -25  # still computed and traced
    assert entry["leave_now_alerted"] is False
    assert entry["leave_now_suppressed"] is True
    assert harness["sent"] == [] and payload["alerts"] == 0


def test_leave_now_fires_for_train_commute_when_toggle_is_off(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _train_office_leg(harness)
    harness["route"] = RouteResult(normal_s=3000, traffic_s=3000)
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    config = dataclasses.replace(
        _config(),
        traffic=TrafficConfig(
            enabled=True, api_key="k", significant_delay_min=15,
            skip_leave_now_for_train=False,
        ),
    )
    payload = traffic_check.run_traffic_check(config, now=NOW, dry_run=False)

    entry = payload["checked"][0]
    assert entry["leave_now_alerted"] is True and entry["leave_now_suppressed"] is False
    assert "Leave now" in harness["sent"][0]


def test_train_suppression_leaves_delay_alert_untouched(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A train-titled event still gets its delay alert — the exemption is scoped
    # to the leave-now nudge only.
    _train_office_leg(harness)
    harness["route"] = RouteResult(normal_s=600, traffic_s=2400)  # 40 min, +30 delay
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    entry = payload["checked"][0]
    assert entry["alerted"] is True and entry["leave_now_alerted"] is False
    assert entry["leave_now_suppressed"] is True
    assert payload["alerts"] == 1 and len(harness["sent"]) == 1
    assert "Traffic alert" in harness["sent"][0]


def test_non_train_commute_is_never_suppressed(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _single_office_leg(harness)
    harness["route"] = RouteResult(normal_s=3000, traffic_s=3000)
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    entry = payload["checked"][0]
    assert entry["leave_now_alerted"] is True
    assert entry["leave_now_suppressed"] is False


def test_disabled_check_is_silent(harness: dict[str, Any]) -> None:
    disabled = dataclasses.replace(_config(), traffic=TrafficConfig(enabled=False, api_key="k"))
    payload = traffic_check.run_traffic_check(disabled, now=NOW, dry_run=False)
    assert payload["status"] == "disabled"
    assert harness["sent"] == []


# ---------------------------------------------------------------- issue #252
#
# Three redundant-alert defects observed in the Telegram channel on 2026-07-30:
# a per-person "Leave now" split, a "Tight schedule" repeating every cycle, and
# that same alert quoting a driving ETA for a train commute.


def _shared_event_for_both(state: dict[str, Any]) -> None:
    """One appointment both parents are heading to (the 2026-07-30 case)."""
    # Starts in 4 min: with a ~1 min drive and the 5 min leave-margin, both
    # people are already past their departure moment — the 08:56-for-09:00
    # shape of the reported alerts.
    event = dict(
        location=WORK, start=NOW + timedelta(minutes=4),
        end=NOW + timedelta(hours=2),
    )
    state["events"] = {
        "roberto": [_event("Medical checkup", eid="a", **event)],
        "ana": [_event("Medical checkup", eid="b", **event)],
    }


def test_leave_now_merges_people_with_different_etas(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two people, one event, different per-person ETAs -> ONE message.

    Reproduces the reported pair: roberto "~1 min" and ana "~0 min" for the
    same 09:00 appointment produced two messages a minute apart, because the
    #213 merge keyed on the ETA - a per-person value.
    """
    _shared_event_for_both(harness)
    etas = iter([RouteResult(normal_s=60, traffic_s=60),    # roberto ~1 min
                 RouteResult(normal_s=0, traffic_s=0)])     # ana      ~0 min

    def per_person_route(origin: str, destination: str, **kw: Any) -> RouteResult:
        harness["route_calls"].append({"origin": origin, "destination": destination})
        return next(etas)

    monkeypatch.setattr(traffic_check, "compute_route", per_person_route)
    monkeypatch.setattr(
        traffic_check, "get_location",
        lambda cfg, person, **kw: _fresh_location(person),
    )

    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    leave_now = [t for t in harness["sent"] if "Leave now" in t]
    assert len(leave_now) == 1, harness["sent"]
    assert "roberto" in leave_now[0] and "ana" in leave_now[0]
    # The larger ETA wins - the nudge has to be right for whoever is furthest out.
    assert "~1 min" in leave_now[0]
    assert payload["alerts"] == 1
    assert all(e["leave_now_alerted"] for e in payload["checked"])


def test_tight_schedule_skipped_for_train_titled_commute(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The infeasible-hop alert is a driving claim, so trains are exempt too.

    The reported message said "Only 0 min between events but the drive is
    ~10 min" for an event explicitly titled "(en tren)".
    """
    start = NOW + timedelta(minutes=30)
    harness["events"] = {
        "roberto": [
            _event("Reunion cliente", location=LUNCH, eid="a",
                   start=NOW - timedelta(hours=1), end=start),
            _event("Trabajo en la oficina (en tren)", location=WORK, eid="b",
                   start=start, end=NOW + timedelta(hours=3)),
        ]
    }
    harness["route"] = RouteResult(normal_s=600, traffic_s=600)  # 10 min drive, 0 min gap

    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    leg = [e for e in payload["checked"] if e["gap_min"] is not None][0]
    assert leg["feasible"] is False          # still judged and traced...
    assert leg["infeasible_suppressed"] is True
    assert leg["alerted"] is False           # ...but not alerted
    assert [t for t in harness["sent"] if "Tight schedule" in t] == []


def test_tight_schedule_still_fires_for_a_normal_drive(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exemption must be scoped to train-titled events only."""
    start = NOW + timedelta(minutes=30)
    harness["events"] = {
        "roberto": [
            _event("Reunion cliente", location=LUNCH, eid="a",
                   start=NOW - timedelta(hours=1), end=start),
            _event("Trabajo en la oficina", location=WORK, eid="b",
                   start=start, end=NOW + timedelta(hours=3)),
        ]
    }
    harness["route"] = RouteResult(normal_s=600, traffic_s=600)

    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    leg = [e for e in payload["checked"] if e["gap_min"] is not None][0]
    assert leg["feasible"] is False and leg["infeasible_suppressed"] is False
    assert leg["alerted"] is True
    assert [t for t in harness["sent"] if "Tight schedule" in t]


def test_run_payload_reports_the_window_actually_applied(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override must be inspectable, not just logged (#252).

    This repo installs no logging handlers, so the `logger.info` announcing the
    raised window is dropped in both the CLI and webapp paths. The run payload
    feeds the Audit tab, so that is where the applied value has to surface.
    """
    _single_office_leg(harness)
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    degenerate = dataclasses.replace(
        _config(),
        traffic=TrafficConfig(
            enabled=True, api_key="k", dedup_window_min=30, cadence_min=30,
            lookahead_hours=3,
        ),
    )

    payload = traffic_check.run_traffic_check(degenerate, now=NOW, dry_run=True)

    assert payload["dedup_window_min"] == 180  # not the configured 30


# ---------------------------------------------------------------- issue #270
#
# The wire format the traffic check actually puts on the Routes API, and the
# departure anchor behind every number it reports.
#
# This is not a paraphrase of `compute_route`'s own contract test: the defect
# #270 fixes lived in the *caller*, which passed `arrival_time` from #160
# onward. Routes accepts that field for a DRIVE route and ignores it, so a
# regression here would not raise, it would quietly re-price every future drive
# as if it started at the sweep moment. Only a test that drives the real
# `compute_route` from `run_traffic_check` and reads the request body can see it.


class _FakeRoutesResponse:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {"routes": [{"duration": "600s", "staticDuration": "600s"}]}


class _RecordingSession:
    """Stands in for the ``requests.Session`` ``run_traffic_check`` opens."""

    def __init__(self, bodies: list[dict[str, Any]]) -> None:
        self.bodies = bodies

    def __enter__(self) -> _RecordingSession:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: int
    ) -> _FakeRoutesResponse:
        self.bodies.append(json)
        return _FakeRoutesResponse()


def _record_wire(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Let the *real* `compute_route` run against a stub session, and record it."""
    from src.traffic import compute_route as real_compute_route

    bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(traffic_check, "compute_route", real_compute_route)
    monkeypatch.setattr(traffic_check.requests, "Session", lambda: _RecordingSession(bodies))
    return bodies


def _no_live_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        traffic_check, "get_location",
        lambda *a, **kw: PresenceUnavailable("roberto", "disabled"),
    )


def test_home_leg_is_priced_as_a_departure_at_the_event_start(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #270 fix: `departureTime`, never `arrivalTime`, on the wire."""
    _single_office_leg(harness)
    _no_live_fix(monkeypatch)
    bodies = _record_wire(monkeypatch)

    payload = traffic_check.run_traffic_check(
        _config(presence_enabled=False), now=NOW, dry_run=True
    )

    assert len(bodies) == 1
    assert "arrivalTime" not in bodies[0]
    assert bodies[0]["departureTime"] == (NOW + timedelta(minutes=30)).astimezone().isoformat()
    # Unchanged on purpose: `staticDuration` (the delay baseline) moves with it.
    assert bodies[0]["routingPreference"] == "TRAFFIC_AWARE"
    assert payload["checked"][0]["anchor"] == "event_start"


def test_chained_leg_is_priced_as_a_departure_at_the_preceding_event_end(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    office_end = NOW + timedelta(minutes=20)
    harness["events"] = {
        "roberto": [
            _event("Office", location=WORK,
                   start=NOW - timedelta(hours=1), end=office_end, eid="a"),
            _event("Lunch", location=LUNCH,
                   start=NOW + timedelta(minutes=30), end=NOW + timedelta(hours=1), eid="b"),
        ]
    }
    _no_live_fix(monkeypatch)
    bodies = _record_wire(monkeypatch)

    payload = traffic_check.run_traffic_check(
        _config(presence_enabled=False), now=NOW, dry_run=True
    )

    assert len(bodies) == 1
    assert "arrivalTime" not in bodies[0]
    assert bodies[0]["departureTime"] == office_end.astimezone().isoformat()
    entry = next(e for e in payload["checked"] if e["event"] == "Lunch")
    assert entry["anchor"] == "preceding_event_end"
    assert entry["departure_anchor"] == office_end.isoformat()


def test_live_presence_leg_sends_no_time_field_at_all(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """"If they leave now, do they make it?" is Routes' no-time-field call.

    The leave-now nudge (#185) asks about a departure at `now`, so the correct
    anchor is no anchor. The pre-#270 `arrival_time` argument was inert here
    rather than wrong — but it is now gone, and the absence is pinned.
    """
    _single_office_leg(harness)
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())
    bodies = _record_wire(monkeypatch)

    payload = traffic_check.run_traffic_check(_config(), now=NOW, dry_run=True)

    assert len(bodies) == 1
    assert "arrivalTime" not in bodies[0] and "departureTime" not in bodies[0]
    entry = payload["checked"][0]
    assert entry["anchor"] == "depart_now" and entry["departure_anchor"] is None


def test_a_leg_whose_own_event_has_started_is_its_own_state_and_costs_no_call(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drive that is already over is a fact, not a transport failure.

    Routes rejects a past `departureTime` outright (`HTTP 400 "Timestamp must be
    set to a future time."`), so sending it would buy a fabricated `routes_error`
    with a billable call. The leg reports what is actually true instead.

    `upcoming_commutes` admits no leg whose event has already started, so this is
    the boundary of that filter — which is exactly what makes it worth pinning:
    the guard has to hold whatever a future anchor derivation does.
    """
    harness["events"] = {
        # Starting this very instant: there is no departure left to price.
        "roberto": [
            _event("Office", location=WORK, start=NOW, end=NOW + timedelta(hours=2), eid="a")
        ]
    }
    _no_live_fix(monkeypatch)
    bodies = _record_wire(monkeypatch)

    payload = traffic_check.run_traffic_check(
        _config(presence_enabled=False), now=NOW, dry_run=False
    )

    assert bodies == [], "a past departure must never be sent, and never billed"
    entry = payload["checked"][0]
    assert entry["status"] == "anchor_in_the_past"
    assert entry["status"] != "error" and "Routes" in entry["detail"]
    assert entry["anchor"] == "event_start"
    assert payload["status"] == "ok" and payload["alerts"] == 0
    assert harness["sent"] == []


def test_late_departure_still_prices_the_hop_and_still_warns(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window the tight-schedule warning exists for must not go silent.

    Office ran until 10 min ago, lunch elsewhere starts in 5, and the drive takes
    25 min. The driver has not left — they are late — so the hop is still ahead
    of them and the warning is more useful now than at any earlier sweep. #282's
    first cut read the elapsed `origin_event_end` as `anchor_in_the_past` and
    dropped the alert; the anchor is `now` instead, sent as no time field so the
    request can never carry a past timestamp.
    """
    harness["events"] = {
        "roberto": [
            _event("Office", location=WORK,
                   start=NOW - timedelta(hours=4), end=NOW - timedelta(minutes=10), eid="a"),
            _event("Lunch", location=LUNCH,
                   start=NOW + timedelta(minutes=5), end=NOW + timedelta(hours=1), eid="b"),
        ]
    }
    harness["route"] = RouteResult(normal_s=1500, traffic_s=1500)  # 25 min, no "delay"
    _no_live_fix(monkeypatch)

    payload = traffic_check.run_traffic_check(
        _config(presence_enabled=False), now=NOW, dry_run=False
    )

    entry = next(e for e in payload["checked"] if e["event"] == "Lunch")
    assert entry["status"] != "anchor_in_the_past"
    assert entry["anchor"] == "depart_now_overdue"
    assert entry["departure_anchor"] is None
    # Priced, and priced depart-now — never a past timestamp on the wire.
    assert len(harness["route_calls"]) == 1
    assert harness["route_calls"][0]["departure_time"] is None
    # `gap_min` is still measured from the preceding event's end, so the
    # feasibility verdict does not move with the anchor that priced the drive.
    assert entry["gap_min"] == 15 and entry["feasible"] is False
    assert entry["alerted"] is True and payload["alerts"] == 1
    assert "Tight schedule" in harness["sent"][0]


def test_delay_alert_names_the_moment_it_was_priced_for(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Now ~40 min" about a drive two hours out is the same lie in words."""
    start = NOW + timedelta(minutes=90)
    harness["events"] = {
        "roberto": [
            _event("Office", location=WORK, start=start, end=start + timedelta(hours=2), eid="a")
        ]
    }
    harness["route"] = RouteResult(normal_s=600, traffic_s=2400)  # +30 min delay
    _no_live_fix(monkeypatch)

    traffic_check.run_traffic_check(_config(presence_enabled=False), now=NOW, dry_run=False)

    assert f"At {start.strftime('%H:%M')} ~40 min" in harness["sent"][0]
    assert "Now ~" not in harness["sent"][0]


def test_leave_now_delay_alert_still_says_now(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A depart-now leg keeps the present tense — it really is about now."""
    _single_office_leg(harness)
    harness["route"] = RouteResult(normal_s=600, traffic_s=2400)
    monkeypatch.setattr(traffic_check, "get_location", lambda *a, **kw: _fresh_location())

    traffic_check.run_traffic_check(_config(), now=NOW, dry_run=False)

    delay = next(t for t in harness["sent"] if "Traffic alert" in t)
    assert "Now ~40 min" in delay
