"""Framework-neutral read-only Google Calendar normalization.

Portable sibling of ``gmail_readonly`` (see ``docs/calendar-bootstrap.md``):
the same installed-app OAuth refresh-token model, scoped to
``calendar.readonly`` only. This module holds the scope constant, the
provider-neutral :class:`CalendarEvent` record, and the pure normalization of
one Google Calendar API v3 event resource — no I/O, no framework imports — so
the deterministic family-schedule logic can be unit-tested against plain dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


@dataclass(frozen=True)
class CalendarEvent:
    """Provider-neutral calendar event produced from a Calendar API resource.

    ``calendar_id`` records which configured calendar (person) the event came
    from. ``start`` / ``end`` are timezone-aware datetimes for timed events; an
    all-day event carries ``all_day=True`` with ``start``/``end`` at local
    midnight. ``video_link`` is the Meet/Zoom/Teams URL when present (used later
    to distinguish a virtual meeting from a hybrid appointment that also carries
    a physical ``location``).
    """

    event_id: str
    calendar_id: str
    summary: str
    location: str
    description: str
    start: datetime
    end: datetime
    all_day: bool
    video_link: str | None
    status: str


class CalendarReadClient(Protocol):
    """Minimal read surface; implementations must expose no writes."""

    def list_events(
        self,
        *,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
    ) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


class CalendarReadError(RuntimeError):
    """A privacy-safe Calendar failure suitable for logs and status surfaces."""


_VIDEO_HINTS = ("meet.google.com", "zoom.us", "teams.microsoft.com", "teams.live.com")


def _parse_boundary(node: dict[str, Any]) -> tuple[datetime, bool]:
    """Return ``(aware datetime, all_day)`` for a Calendar start/end node."""
    raw_datetime = node.get("dateTime")
    if raw_datetime:
        # Calendar emits RFC-3339; Python 3.11+ parses the trailing 'Z'.
        text = str(raw_datetime).replace("Z", "+00:00")
        return datetime.fromisoformat(text), False
    raw_date = node.get("date")
    if raw_date:
        day = date.fromisoformat(str(raw_date))
        return datetime(day.year, day.month, day.day), True
    raise ValueError("Calendar event boundary has neither dateTime nor date")


def _video_link(raw: dict[str, Any]) -> str | None:
    """Best-effort Meet/Zoom/Teams URL from hangoutLink/conferenceData/text."""
    hangout = raw.get("hangoutLink")
    if hangout:
        return str(hangout)
    conference = raw.get("conferenceData") or {}
    for entry in conference.get("entryPoints") or []:
        uri = entry.get("uri")
        if uri and any(hint in str(uri) for hint in _VIDEO_HINTS):
            return str(uri)
    haystack = f"{raw.get('location') or ''} {raw.get('description') or ''}"
    lowered = haystack.lower()
    if any(hint in lowered for hint in _VIDEO_HINTS):
        return "video-link"  # present but not extractable to a clean URL
    return None


def normalize_event(raw: dict[str, Any], *, calendar_id: str) -> CalendarEvent:
    """Normalize one Calendar API v3 event resource into a :class:`CalendarEvent`."""
    event_id = str(raw.get("id") or "")
    if not event_id:
        raise ValueError("Calendar event has no id")
    start, start_all_day = _parse_boundary(raw.get("start") or {})
    end, end_all_day = _parse_boundary(raw.get("end") or {})
    return CalendarEvent(
        event_id=event_id,
        calendar_id=calendar_id,
        summary=str(raw.get("summary") or "").strip(),
        location=str(raw.get("location") or "").strip(),
        description=str(raw.get("description") or "").strip(),
        start=start,
        end=end,
        all_day=start_all_day or end_all_day,
        video_link=_video_link(raw),
        status=str(raw.get("status") or "confirmed"),
    )


def _safe_error_detail(exc: Exception) -> str:
    """A privacy-safe error string that never echoes calendar content."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 401:
        return "OAuth token is invalid or expired"
    if status == 403:
        return "Calendar API permission or quota denied"
    if status == 429:
        return "Calendar API quota exceeded"
    if isinstance(exc, FileNotFoundError):
        return str(exc)
    return f"Calendar API request failed ({type(exc).__name__})"
