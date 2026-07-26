"""Framework-neutral write-scope Google Calendar constants (#217).

Sibling of ``calendar_readonly`` (see ``docs/calendar-bootstrap.md``) — but a
genuinely separate credential/scope boundary, since Google Calendar OAuth
scopes cannot be upgraded in place on an existing token. Deliberately minimal:
insert + delete only, the narrowest scope (``calendar.events``) that permits
event creation without full calendar management (``calendar`` would also grant
calendar-list/ACL changes). No event normalization lives here — unlike
``calendar_readonly.core``, writes only ever construct events, never parse one
back.
"""

from __future__ import annotations

from typing import Any, Protocol

CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class CalendarWriteClient(Protocol):
    """Minimal write surface — insert + delete only (#217; keep minimal)."""

    def insert_event(self, *, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]: ...

    def delete_event(self, *, calendar_id: str, event_id: str) -> None: ...

    def close(self) -> None: ...


class CalendarWriteError(RuntimeError):
    """A privacy-safe Calendar write failure suitable for logs and status surfaces."""
