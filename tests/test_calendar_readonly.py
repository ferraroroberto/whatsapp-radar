"""Portable offline contract tests for the root-level Calendar component (#241).

Regression coverage for the naive/aware datetime bug: an all-day event's
``start``/``end`` must come back timezone-aware, matching
``CalendarEvent``'s and ``_parse_boundary``'s own documented contract, so
downstream tz-aware comparisons (e.g. the per-day scan window filter in
``src/family/calendar_scan.py``) never raise
``TypeError: can't compare offset-naive and offset-aware datetimes``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from calendar_readonly.core import normalize_event

_RAW_ALL_DAY = {
    "id": "allday1",
    "summary": "Public holiday",
    "start": {"date": "2026-07-21"},
    "end": {"date": "2026-07-22"},
}

_RAW_TIMED = {
    "id": "timed1",
    "summary": "Dentist",
    "start": {"dateTime": "2026-07-21T10:00:00+02:00"},
    "end": {"dateTime": "2026-07-21T11:00:00+02:00"},
}


def test_all_day_event_start_and_end_are_timezone_aware() -> None:
    event = normalize_event(_RAW_ALL_DAY, calendar_id="parent@example.com")
    assert event.all_day is True
    assert event.start.tzinfo is not None
    assert event.end.tzinfo is not None
    # Local midnight, not shifted by the tz attachment.
    assert (event.start.hour, event.start.minute, event.start.second) == (0, 0, 0)
    assert (event.end.hour, event.end.minute, event.end.second) == (0, 0, 0)


def test_all_day_event_comparable_against_aware_bounds() -> None:
    """The exact crash from #241: comparing an all-day start against tz-aware bounds."""
    event = normalize_event(_RAW_ALL_DAY, calendar_id="parent@example.com")
    day_min = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)
    day_max = datetime(2026, 7, 22, 0, 0, tzinfo=UTC)
    # Must not raise TypeError: can't compare offset-naive and offset-aware datetimes.
    assert (day_min <= event.start < day_max) in (True, False)


def test_timed_event_start_and_end_are_timezone_aware() -> None:
    event = normalize_event(_RAW_TIMED, calendar_id="parent@example.com")
    assert event.all_day is False
    assert event.start.tzinfo is not None
    assert event.end.tzinfo is not None
