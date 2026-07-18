"""Portable read-only Google Calendar component (issue #160).

Sibling of ``gmail_readonly``: installed-app OAuth refresh-token flow scoped to
``calendar.readonly`` only, plus a narrow read client and provider-neutral event
normalization. Bootstrap/runbook: ``docs/calendar-bootstrap.md``.
"""

from calendar_readonly.core import (
    CALENDAR_READONLY_SCOPE,
    CalendarEvent,
    CalendarReadClient,
    CalendarReadError,
    normalize_event,
)
from calendar_readonly.google_client import (
    GoogleCalendarReadClient,
    build_google_calendar_client,
)

__all__ = [
    "CALENDAR_READONLY_SCOPE",
    "CalendarEvent",
    "CalendarReadClient",
    "CalendarReadError",
    "GoogleCalendarReadClient",
    "build_google_calendar_client",
    "normalize_event",
]
