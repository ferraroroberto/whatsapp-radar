"""Fetch upcoming events per person from the configured household calendars (#160).

The one I/O seam the two deterministic checks share: build the read-only
Calendar client once, list each configured calendar over a window, and return
normalized events keyed by person. Everything downstream is pure.
"""

from __future__ import annotations

from datetime import datetime

from calendar_readonly.core import CalendarEvent, normalize_event
from calendar_readonly.google_client import build_google_calendar_client

from src.config import CalendarConfig


def fetch_events_by_person(
    calendar: CalendarConfig,
    *,
    time_min: datetime,
    time_max: datetime,
) -> dict[str, list[CalendarEvent]]:
    """Return ``{person: [events]}`` for every configured calendar in the window."""
    client = build_google_calendar_client(calendar.token_path)
    try:
        by_person: dict[str, list[CalendarEvent]] = {}
        for account in calendar.accounts:
            raw_events = client.list_events(
                calendar_id=account.calendar_id, time_min=time_min, time_max=time_max
            )
            by_person.setdefault(account.person, []).extend(
                normalize_event(raw, calendar_id=account.calendar_id) for raw in raw_events
            )
        return by_person
    finally:
        client.close()
