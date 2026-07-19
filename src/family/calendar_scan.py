"""Daily calendar-conflict scan (issue #160) — deterministic, one-shot.

Pipeline: fetch the next few days for both calendars → per-event decision trace
(#168: every event gets a recorded location verdict with its reason; a missing
location is *assumed home* and flagged, never silently dropped) → apply the
fixed weekly responsibility pattern plus two-places-at-once detection over the
assessment window → always send one summary on a live run: coverage issues and
missing-location asks, or an explicit all-clear. Coverage gaps and overlaps are
hard alerts and bypass quiet hours; a clean summary inside quiet hours is
suppressed until the next daytime run. Returns a schema-stable result payload
the CLI persists as the run's ``summary_json`` (#163) and prints.
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


def _fmt_when(iso_start: str) -> str:
    try:
        moment = datetime.fromisoformat(iso_start)
    except ValueError:
        return iso_start
    return moment.strftime("%a %d %b %H:%M")


def build_summary_text(
    *,
    scan_days: int,
    assessment_days: int,
    conflicts: list[dict[str, Any]],
    missing: list[dict[str, Any]],
) -> str:
    """Compose the daily Telegram summary — always produced, even all-clear.

    Per-person coverage issues come first (they are the hard alerts), then the
    missing-location asks grouped by person, then the explicit all-clear when
    there is nothing at all — silence is never a result (#168).
    """
    lines = [f"📅 Calendar sync — checked the next {scan_days} days."]
    if conflicts:
        lines.append(f"⚠️ {len(conflicts)} issue(s) in the next {assessment_days} day(s):")
        lines.extend(f"• {c['detail']}" for c in conflicts[:6])
        if len(conflicts) > 6:
            lines.append(f"…and {len(conflicts) - 6} more.")
    if missing:
        lines.append("📍 No location set — please add one:")
        by_person: dict[str, list[str]] = {}
        # Sorted by (person, start) so the same inputs always compose the same
        # message regardless of fetch order — determinism is part of the spec.
        for item in sorted(missing, key=lambda m: (str(m["person"]), str(m["start"]))):
            by_person.setdefault(item["person"], []).append(
                f"{_fmt_when(item['start'])} — {item['event']}"
            )
        for person, items in sorted(by_person.items()):
            for entry in items[:4]:
                lines.append(f"• {person}: {entry}")
            if len(items) > 4:
                lines.append(f"• {person}: …and {len(items) - 4} more.")
    if not conflicts and not missing:
        lines.append("✅ Everything is fine — nothing needs your attention.")
    return "\n".join(lines)


def run_calendar_scan(config: Config, *, now: datetime, dry_run: bool) -> dict[str, Any]:
    """Run one calendar sync. ``dry_run`` never sends anything."""
    family = config.family
    if not family.enabled and not dry_run:
        return {"kind": "calendar-scan", "status": "disabled", "conflicts": [],
                "missing_locations": [], "decisions": [],
                "summary": {"status": "skipped", "reason": "disabled"}}

    # One fetch over the full missing-location window covers the assessment days.
    midnight = datetime.combine(now.date(), time.min).astimezone(now.tzinfo)
    scan_days = max(family.unknown_scan_days, family.assessment_days)
    window_end = midnight + timedelta(days=scan_days)
    events = fetch_events_by_person(config.calendar, time_min=midnight, time_max=window_end)

    decisions = rules.event_decisions(events, home_address=family.home_address)
    missing = [
        {"person": person, "event": event.summary, "start": event.start.isoformat()}
        for person, event in rules.find_missing_locations(
            events, home_address=family.home_address
        )
    ]

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
        conflicts.extend(
            rules.find_overlaps(day_events, home_address=family.home_address)
        )

    conflict_dicts = [{"kind": c.kind, "day": c.day, "detail": c.detail} for c in conflicts]
    text = build_summary_text(
        scan_days=scan_days,
        assessment_days=family.assessment_days,
        conflicts=conflict_dicts,
        missing=missing,
    )

    # Always-send contract (#168): a live run produces exactly one summary —
    # findings or the explicit all-clear. Hard alerts (any conflict) bypass
    # quiet hours; a clean/asks-only summary inside quiet hours is suppressed.
    quiet = rules.in_quiet_hours(
        now, config.traffic.quiet_start_hour, config.traffic.quiet_end_hour
    )
    summary: dict[str, Any] = {"text": text}
    if dry_run:
        summary["status"] = "dry_run"
    elif quiet and not conflicts:
        summary["status"] = "suppressed_quiet_hours"
    else:
        status, detail = send_alert(config, text)
        summary["status"] = status
        if detail:
            summary["detail"] = detail

    return {
        "kind": "calendar-scan", "status": "ok",
        "conflicts": conflict_dicts,
        "missing_locations": missing,
        "decisions": decisions,
        "summary": summary,
        "dry_run": dry_run,
    }
