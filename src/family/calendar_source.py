"""Fetch upcoming events per person from the configured household calendars (#160).

The one I/O seam the two deterministic checks share: build the read-only
Calendar client once, list each configured calendar over a window, and return
normalized events keyed by person. Everything downstream is pure.

Being the *only* place ``normalize_event`` is called for the family flows, it
is also where the travel-block feedback-loop guard belongs (#265).

:func:`fetch_marked_events` (#267) is the deliberately *separate* seam the
travel-block reconcile reads through: it lists only the events carrying a given
``extendedProperties.private`` marker, so it never sees a human's event at all
and never needs that guard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from calendar_readonly.core import CalendarEvent, normalize_event, safe_error_detail
from calendar_readonly.google_client import build_google_calendar_client

from src.config import CalendarConfig
from src.family.travel_blocks import is_travel_block

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class MarkedEvents:
    """One marker-scoped read pass over the household calendars (#267).

    Three separate facts, kept separate on purpose:

    * ``marked`` — the raw event resources carrying the marker, per calendar id.
      Raw, not normalized: the caller's delete path backs up and re-verifies the
      *whole fetched resource*, and normalization would throw away most of it.
    * ``unreadable`` — calendars whose listing failed, and why. Such a calendar
      is emphatically not "a calendar with no blocks": treating an unknown
      current state as empty is what would duplicate every block on it.
    * ``access_roles`` — the calendar-list ``accessRole`` per calendar, or
      ``None`` when it could not be established. ``None`` is the third state, not
      a synonym for "no access".
    """

    marked: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)
    access_roles: dict[str, str | None] = field(default_factory=dict)


def fetch_marked_events(
    calendar: CalendarConfig,
    *,
    time_min: datetime,
    time_max: datetime,
    private_extended_property: str,
) -> MarkedEvents:
    """List the events carrying ``private_extended_property`` on every configured calendar.

    The filter is applied by Google, not here: a caller reconciling events it
    wrote itself never fetches anybody else's event, which is a far stronger
    guarantee than filtering the response locally would be.

    Each calendar is read independently and a failure degrades that calendar
    alone — one broken account must not blind the reconcile to the others. The
    non-mutating write-capability probe rides along in the same pass so the
    reconcile does not need a second client (or a write-then-delete round trip)
    to learn whether it may write.
    """
    client = build_google_calendar_client(calendar.token_path)
    marked: dict[str, list[dict[str, Any]]] = {}
    unreadable: dict[str, str] = {}
    access_roles: dict[str, str | None] = {}
    try:
        # `dict.fromkeys` keeps configuration order while collapsing a calendar
        # id configured twice (two labels, one calendar) into one round trip.
        for calendar_id in dict.fromkeys(account.calendar_id for account in calendar.accounts):
            try:
                marked[calendar_id] = client.list_events(
                    calendar_id=calendar_id,
                    time_min=time_min,
                    time_max=time_max,
                    private_extended_property=private_extended_property,
                )
            except Exception as exc:  # noqa: BLE001 — one calendar's failure is not the run's
                detail = safe_error_detail(exc)
                unreadable[calendar_id] = detail
                logger.warning(
                    "⚠️ could not list marked events on %s: %s — the calendar's current "
                    "contents are unknown, not empty",
                    calendar_id,
                    detail,
                )
            try:
                access_roles[calendar_id] = client.calendar_access_role(calendar_id)
            except Exception as exc:  # noqa: BLE001 — an unresolved probe is its own state
                access_roles[calendar_id] = None
                logger.warning(
                    "⚠️ could not read the access role for %s: %s — write capability "
                    "stays unknown",
                    calendar_id,
                    safe_error_detail(exc),
                )
    finally:
        client.close()
    return MarkedEvents(marked=marked, unreadable=unreadable, access_roles=access_roles)
