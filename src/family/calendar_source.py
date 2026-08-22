"""Fetch upcoming events per person from the configured household calendars (#160).

The one I/O seam the two deterministic checks share: build the read-only
Calendar client once, list each configured calendar over a window, and return
normalized events keyed by person. Everything downstream is pure.

Being the *only* place ``normalize_event`` is called for the family flows, it
is also where the travel-block feedback-loop guard belongs (#265).
"""

from __future__ import annotations

from datetime import datetime

from calendar_readonly.core import CalendarEvent, normalize_event
from calendar_readonly.google_client import build_google_calendar_client

from src.config import CalendarConfig
from src.family.travel_blocks import is_travel_block


def fetch_events_by_person(
    calendar: CalendarConfig,
    *,
    time_min: datetime,
    time_max: datetime,
) -> dict[str, list[CalendarEvent]]:
    """Return ``{person: [events]}`` for every configured calendar in the window.

    Travel blocks this app wrote itself are dropped here — see
    :func:`src.family.travel_blocks.is_travel_block` for why the guard lives at
    this single seam rather than at each of the five consumers. A configured
    person always gets a key, even when every one of their events was filtered
    out: ``find_conflicts`` reasons about who is *available*, so a silently
    absent person would read as "not in the household".
    """
    client = build_google_calendar_client(calendar.token_path)
    try:
        by_person: dict[str, list[CalendarEvent]] = {}
        for account in calendar.accounts:
            raw_events = client.list_events(
                calendar_id=account.calendar_id, time_min=time_min, time_max=time_max
            )
            person_events = by_person.setdefault(account.person, [])
            person_events.extend(
                event
                for event in (
                    normalize_event(raw, calendar_id=account.calendar_id) for raw in raw_events
                )
                if not is_travel_block(event)
            )
        return by_person
    finally:
        client.close()
