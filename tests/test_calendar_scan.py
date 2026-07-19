"""Calendar-sync summary contract (#168): always audible, never silent.

Offline — the calendar fetch and the alert sender are stubbed. Sanitized
fixture events only (generic names). Covers the four send paths: findings sent,
explicit all-clear sent, quiet-hours suppression for a routine summary, and the
hard-alert bypass when a coverage issue lands inside quiet hours — plus dry-run
producing the text without sending, and the decision trace riding the payload.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from calendar_readonly.core import CalendarEvent

from src.config import Config, FamilyConfig, HubConfig, TelegramConfig, TrafficConfig
from src.family import calendar_scan

HOME = "Carrer Example 30, Sant Cugat"
WORK = "Avenida Diagonal 621, Barcelona"

# Daytime, outside the default 20..5 quiet window; a Monday.
DAY_NOW = datetime(2026, 7, 20, 7, 5, tzinfo=UTC)
NIGHT_NOW = datetime(2026, 7, 20, 22, 0, tzinfo=UTC)


def _event(
    summary: str,
    *,
    location: str = "",
    start: datetime,
    end: datetime | None = None,
    eid: str = "e1",
) -> CalendarEvent:
    return CalendarEvent(
        event_id=eid,
        calendar_id="parent@example.com",
        summary=summary,
        location=location,
        description="",
        start=start,
        end=end or (start + timedelta(hours=1)),
        all_day=False,
        video_link=None,
        status="confirmed",
    )


def _config() -> Config:
    return Config(
        db_path="unused.sqlite3",  # type: ignore[arg-type]
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="m"),
        notifier="telegram",
        telegram=TelegramConfig(bot_token="t", chat_id="c"),
        linked_device_dir="ld",  # type: ignore[arg-type]
        traffic=TrafficConfig(),
        family=FamilyConfig(
            enabled=True,
            home_address=HOME,
            kids_home_time="17:30",
            responsible_by_weekday={i: "roberto" for i in range(7)},
            unknown_scan_days=7,
            assessment_days=2,
        ),
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture alert texts; calendar fetch defaults to an empty week."""
    outbox: list[str] = []

    def fake_send(config: Config, text: str) -> tuple[str, str | None]:
        outbox.append(text)
        return "sent", None

    monkeypatch.setattr(calendar_scan, "send_alert", fake_send)
    monkeypatch.setattr(
        calendar_scan, "fetch_events_by_person", lambda *a, **kw: {"roberto": [], "ana": []}
    )
    return outbox


def _with_events(
    monkeypatch: pytest.MonkeyPatch, events: dict[str, list[CalendarEvent]]
) -> None:
    monkeypatch.setattr(calendar_scan, "fetch_events_by_person", lambda *a, **kw: events)


def test_all_clear_is_still_sent(sent: list[str]) -> None:
    payload = calendar_scan.run_calendar_scan(_config(), now=DAY_NOW, dry_run=False)
    assert payload["summary"]["status"] == "sent"
    assert len(sent) == 1
    assert "Everything is fine" in sent[0]
    assert "Calendar sync" in sent[0]


def test_findings_sent_with_missing_location_ask(
    sent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Responsible parent away over kids-home time + one no-location event.
    away = _event(
        "Offsite", location=WORK,
        start=DAY_NOW.replace(hour=17, minute=0), eid="a",
    )
    mystery = _event(
        "Mystery appointment", start=DAY_NOW.replace(hour=11, minute=0), eid="b"
    )
    _with_events(monkeypatch, {"roberto": [away, mystery], "ana": []})
    payload = calendar_scan.run_calendar_scan(_config(), now=DAY_NOW, dry_run=False)
    assert payload["summary"]["status"] == "sent"
    text = sent[0]
    assert "coverage" in text or "issue(s)" in text
    assert "No location set" in text
    assert "Mystery appointment" in text
    assert [m["event"] for m in payload["missing_locations"]] == ["Mystery appointment"]
    # The trace records the home assumption for the flagged event.
    traced = {d["event"]: d for d in payload["decisions"]}
    assert traced["Mystery appointment"]["assumed"] is True
    assert traced["Offsite"]["kind"] == "away"


def test_quiet_hours_suppresses_routine_summary(sent: list[str]) -> None:
    payload = calendar_scan.run_calendar_scan(_config(), now=NIGHT_NOW, dry_run=False)
    assert payload["summary"]["status"] == "suppressed_quiet_hours"
    assert payload["summary"]["text"]  # composed anyway, visible in the run record
    assert sent == []


def test_hard_alert_bypasses_quiet_hours(
    sent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = NIGHT_NOW.replace(hour=17, minute=0)
    overlap_a = _event("Dentist", location=WORK, start=t0, eid="a")
    overlap_b = _event(
        "Recital", location="Carrer de la Marina 16, Barcelona",
        start=t0 + timedelta(minutes=30), eid="b",
    )
    _with_events(monkeypatch, {"ana": [overlap_a, overlap_b], "roberto": []})
    payload = calendar_scan.run_calendar_scan(_config(), now=NIGHT_NOW, dry_run=False)
    assert any(c["kind"] == "impossible_overlap" for c in payload["conflicts"])
    assert payload["summary"]["status"] == "sent"  # bypassed quiet hours
    assert "two places at once" in sent[0]


def test_dry_run_composes_but_never_sends(sent: list[str]) -> None:
    payload = calendar_scan.run_calendar_scan(_config(), now=DAY_NOW, dry_run=True)
    assert payload["summary"]["status"] == "dry_run"
    assert "Everything is fine" in payload["summary"]["text"]
    assert sent == []


def test_disabled_stays_silent(sent: list[str]) -> None:
    disabled = dataclasses.replace(
        _config(), family=FamilyConfig(enabled=False, home_address=HOME)
    )
    payload = calendar_scan.run_calendar_scan(disabled, now=DAY_NOW, dry_run=False)
    assert payload["status"] == "disabled"
    assert payload["summary"]["status"] == "skipped"
    assert sent == []


def test_summary_text_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = [
        {"person": "ana", "event": "Hairdresser", "start": "2026-07-21T15:30:00+00:00"},
        {"person": "roberto", "event": "Revision", "start": "2026-07-20T17:00:00+00:00"},
    ]
    text_one = calendar_scan.build_summary_text(
        scan_days=7, assessment_days=2, conflicts=[], missing=missing
    )
    text_two = calendar_scan.build_summary_text(
        scan_days=7, assessment_days=2, conflicts=[], missing=list(reversed(missing))
    )
    # Grouped by person, sorted — input order never changes the message.
    assert text_one.splitlines()[1] == "📍 No location set — please add one:"
    assert "ana: Tue 21 Jul 15:30 — Hairdresser" in text_one
    assert text_one == text_two


# --------------------------------------------------------------- live coverage (#177)

LIVE_NOW = datetime(2026, 7, 20, 16, 0, tzinfo=UTC)  # Monday; kids-home 17:30 imminent
PLACEHOLDER_LATLNG = (41.55512, 2.34567)  # generic decimals, never a real location


def _live_config() -> Config:
    from src.config import PresenceConfig

    return dataclasses.replace(
        _config(),
        presence=PresenceConfig(enabled=True),
        traffic=TrafficConfig(api_key="test-key"),
    )


def _fix(at_home: bool = False) -> object:
    from src.presence import PresenceLocation

    return PresenceLocation(
        person="roberto", latitude=PLACEHOLDER_LATLNG[0], longitude=PLACEHOLDER_LATLNG[1],
        at_home=at_home, distance_from_home_km=42.0, age_min=2.0, refreshed=False,
    )


def _route(minutes: int) -> object:
    from src.traffic import RouteResult

    return RouteResult(normal_s=minutes * 60, traffic_s=minutes * 60)


def test_live_infeasible_window_raises_coverage_eta(
    sent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phone 120 min out, window in 90 → coverage_eta conflict, hard-alerted."""
    monkeypatch.setattr(calendar_scan, "get_location", lambda *a, **kw: _fix())
    monkeypatch.setattr(calendar_scan, "compute_route", lambda *a, **kw: _route(120))
    payload = calendar_scan.run_calendar_scan(_live_config(), now=LIVE_NOW, dry_run=False)
    kinds = [c["kind"] for c in payload["conflicts"]]
    assert "coverage_eta" in kinds
    assert any("min short" in c["detail"] for c in payload["conflicts"])
    assert sent, "an infeasible window is a hard alert and must send"
    entry = next(e for e in payload["live_coverage"] if e.get("window") == "kids home")
    assert entry["location_source"] == "live_presence"
    assert entry["feasible"] is False and entry["margin_min"] == -30
    # Privacy: derived values only — never coordinates, in any key or value.
    import json

    text = json.dumps(payload)
    assert "latitude" not in text and "longitude" not in text
    assert str(PLACEHOLDER_LATLNG[0]) not in text and str(PLACEHOLDER_LATLNG[1]) not in text


def test_live_feasible_window_adds_trace_without_conflict(
    sent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calendar_scan, "get_location", lambda *a, **kw: _fix())
    monkeypatch.setattr(calendar_scan, "compute_route", lambda *a, **kw: _route(20))
    payload = calendar_scan.run_calendar_scan(_live_config(), now=LIVE_NOW, dry_run=False)
    assert payload["conflicts"] == []
    entry = next(e for e in payload["live_coverage"] if e.get("window") == "kids home")
    assert entry["feasible"] is True and entry["margin_min"] == 70
    assert entry["eta_min"] == 20


def test_live_at_home_skips_routing(
    sent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: object, **kw: object) -> object:
        raise AssertionError("compute_route must not be called when already home")

    monkeypatch.setattr(calendar_scan, "get_location", lambda *a, **kw: _fix(at_home=True))
    monkeypatch.setattr(calendar_scan, "compute_route", _boom)
    payload = calendar_scan.run_calendar_scan(_live_config(), now=LIVE_NOW, dry_run=False)
    entry = next(e for e in payload["live_coverage"] if e.get("window") == "kids home")
    assert entry["feasible"] is True and entry["at_home"] is True and entry["eta_min"] == 0


def test_live_presence_unavailable_falls_back_silently(
    sent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.presence import PresenceUnavailable

    monkeypatch.setattr(
        calendar_scan,
        "get_location",
        lambda *a, **kw: PresenceUnavailable("roberto", "transport_error"),
    )
    payload = calendar_scan.run_calendar_scan(_live_config(), now=LIVE_NOW, dry_run=False)
    assert payload["conflicts"] == []
    assert payload["live_coverage"] == [
        {
            "person": "roberto", "location_source": "calendar_inference",
            "presence_status": "transport_error", "assessed": False,
            "windows": ["kids home"],
        }
    ]


def test_live_coverage_absent_when_presence_disabled(sent: list[str]) -> None:
    payload = calendar_scan.run_calendar_scan(_config(), now=LIVE_NOW, dry_run=False)
    assert payload["live_coverage"] == []
