"""Daily calendar-conflict scan (issue #160) — deterministic, one-shot.

Pipeline: fetch the next few days for both calendars → a 7-day unknown-location
pre-check (Unknown is asked about, never guessed) → apply the fixed weekly
responsibility pattern over the 2-day assessment window → alert only on genuine
coverage gaps. Returns a schema-stable result payload the CLI prints as the
run's structured result.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

from src.config import Config
from src.family import rules
from src.family.calendar_source import fetch_events_by_person
from src.notify.alert import send_alert


def _day_bounds(day_start: datetime) -> tuple[datetime, datetime]:
    return day_start, day_start + timedelta(days=1)


def run_calendar_scan(config: Config, *, now: datetime, dry_run: bool) -> dict[str, Any]:
    """Run one daily conflict scan. ``dry_run`` never sends anything."""
    family = config.family
    if not family.enabled and not dry_run:
        return {"kind": "calendar-scan", "status": "disabled", "conflicts": [],
                "unknown_locations": []}

    # One fetch over the full unknown-pre-check window covers the 2-day assessment.
    midnight = datetime.combine(now.date(), time.min).astimezone(now.tzinfo)
    window_end = midnight + timedelta(days=max(family.unknown_scan_days, family.assessment_days))
    events = fetch_events_by_person(config.calendar, time_min=midnight, time_max=window_end)

    unknowns = rules.find_unknown_locations(events, home_address=family.home_address)

    conflicts: list[rules.Conflict] = []
    for offset in range(family.assessment_days):
        day = now.date() + timedelta(days=offset)
        day_min, day_max = _day_bounds(datetime.combine(day, time.min).astimezone(now.tzinfo))
        day_events = {
            person: [e for e in evs if day_min <= e.start < day_max]
            for person, evs in events.items()
        }
        conflicts.extend(
            rules.find_conflicts(day_events, family, day=day, tz=now.tzinfo or UTC)
        )

    if conflicts and not dry_run:
        lines = "\n".join(f"• {c.detail}" for c in conflicts[:6])
        send_alert(config, f"📅 Family schedule — {len(conflicts)} coverage issue(s):\n{lines}")

    return {
        "kind": "calendar-scan", "status": "ok",
        "conflicts": [{"kind": c.kind, "day": c.day, "detail": c.detail} for c in conflicts],
        "unknown_locations": [
            {"person": person, "event": event.summary, "start": event.start.isoformat()}
            for person, event in unknowns
        ],
        "dry_run": dry_run,
    }
