"""Routine-prep -> calendar reminder creation (#218, Step 4/5 of #206).

Additive side effect alongside the existing Telegram digest: once a Stage-2
item resolves as action-required, routine-complexity, with a known child and
deadline date, this creates one morning-of-deadline calendar event via the
Step 3 write adapter (``calendar_write``) and returns the created event id to
persist on the item's ``analysis_items`` row. Never raises — a missing/invalid
write token, an unconfigured reminder calendar, or a Google API failure all
degrade to "no calendar event created"; the Telegram digest path is untouched.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any

from calendar_write import CalendarWriteClient, build_google_calendar_write_client

from src.analysis.contract import AnalysisResult
from src.config import CalendarConfig, FamilyConfig
from src.db import store

logger = logging.getLogger(__name__)

# A short reminder block, not a real appointment — just long enough to show up
# clearly on a morning calendar view.
_REMINDER_DURATION_MINUTES = 15


def is_eligible(result: AnalysisResult) -> bool:
    """Whether ``result`` qualifies for a routine-prep reminder event.

    Never guesses: a non-routine item, an unresolved child, or a missing
    deadline date all fall back to today's Telegram-only behavior (#218).
    """
    return (
        result.action_required
        and result.prep_complexity == "routine"
        and bool(result.child)
        and bool(result.deadline_date)
    )


def build_client(calendar: CalendarConfig) -> CalendarWriteClient:
    """Build the real write-scope client.

    Raises ``FileNotFoundError``/``RuntimeError`` (token not yet minted, or
    invalid/revoked) for the caller to catch and degrade gracefully — see
    ``scripts/auth_calendar_write.py`` (#217).
    """
    return build_google_calendar_write_client(calendar.write_token_path)


def build_reminder_event(
    family: FamilyConfig, result: AnalysisResult, chat_display_name: str
) -> dict[str, Any]:
    """The Google Calendar event body for one routine-prep morning reminder."""
    assert result.deadline_date is not None
    assert result.child is not None
    day = date.fromisoformat(result.deadline_date)
    hour_str, _, minute_str = family.reminder_time.partition(":")
    start = datetime.combine(day, time(int(hour_str), int(minute_str))).astimezone()
    end = start + timedelta(minutes=_REMINDER_DURATION_MINUTES)
    title = f"{result.child}: {result.task_category or 'school prep'}"
    description = "\n".join(
        line
        for line in (result.summary, result.suggested_next_action, f"Source: {chat_display_name}")
        if line
    )
    return {
        "summary": title,
        "description": description,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }


def create_reminder_event(
    conn: sqlite3.Connection,
    client: CalendarWriteClient,
    *,
    calendar_id: str,
    family: FamilyConfig,
    chat_id: int,
    item_id: int,
    chat_display_name: str,
    result: AnalysisResult,
) -> str | None:
    """Create (or reuse) one reminder event for ``result``; never raises.

    Idempotent across reprocessing/replays: identity is the evidence-message-id
    set, not the run, so the same underlying item never mints a second event
    even if it is reclassified in a later run (#218).
    """
    existing = store.find_calendar_event_for_evidence(conn, chat_id, result.evidence_message_ids)
    if existing is not None:
        store.set_calendar_event_id(conn, item_id, existing)
        return existing

    event = build_reminder_event(family, result, chat_display_name)
    try:
        created = client.insert_event(calendar_id=calendar_id, event=event)
    except Exception as exc:  # noqa: BLE001 — a calendar failure must never break the scan
        logger.warning("⚠️ calendar reminder creation failed for chat %s: %s", chat_id, exc)
        return None

    event_id = str(created.get("id") or "") or None
    if event_id:
        store.set_calendar_event_id(conn, item_id, event_id)
    return event_id
