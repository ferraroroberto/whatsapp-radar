"""Portable write-scope Google Calendar component (#217).

Sibling of ``calendar_readonly``: a separate credential/scope boundary, since
Google Calendar OAuth scopes cannot be upgraded in place on an existing token.
Deliberately minimal — insert + delete only, the narrowest scope
(``calendar.events``) that permits event creation without full calendar
management. Bootstrap/runbook: ``docs/calendar-bootstrap.md``.
"""

from calendar_write.core import (
    CALENDAR_WRITE_SCOPE,
    CalendarWriteClient,
    CalendarWriteError,
)
from calendar_write.google_client import (
    GoogleCalendarWriteClient,
    build_google_calendar_write_client,
)

__all__ = [
    "CALENDAR_WRITE_SCOPE",
    "CalendarWriteClient",
    "CalendarWriteError",
    "GoogleCalendarWriteClient",
    "build_google_calendar_write_client",
]
