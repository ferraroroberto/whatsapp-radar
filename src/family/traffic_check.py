"""Traffic-jam insurance check (issue #160) — deterministic, one-shot.

Pipeline: quiet-hours gate → fetch upcoming events → classify commutes →
resolve back-to-back origins → one Routes call per leg → dedup → alert only on a
significant delay. Returns a schema-stable result payload (one entry per route
checked, always-present ``dedup_key``, one timestamp format, real API output
only) that the CLI prints as the run's structured result.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from src.config import Config
from src.family import dedup, rules
from src.family.calendar_source import fetch_events_by_person
from src.notify.alert import send_alert
from src.traffic import RouteResult, TrafficReadError, compute_route, delay_status


def _alert_text(person: str, leg_summary: str, result: RouteResult, delay: int) -> str:
    return (
        f"🚗 Traffic alert — {person}: “{leg_summary}”. "
        f"Now ~{result.traffic_s // 60} min vs {result.normal_s // 60} min normal "
        f"(+{delay} min). Leave earlier."
    )


def run_traffic_check(config: Config, *, now: datetime, dry_run: bool) -> dict[str, Any]:
    """Run one traffic-jam check. ``dry_run`` never sends or records anything."""
    traffic = config.traffic
    if not traffic.enabled and not dry_run:
        return {"kind": "traffic-check", "status": "disabled", "checked": [], "alerts": 0}
    if rules.in_quiet_hours(now, traffic.quiet_start_hour, traffic.quiet_end_hour):
        return {"kind": "traffic-check", "status": "quiet_hours", "checked": [], "alerts": 0}
    if not traffic.api_key:
        return {"kind": "traffic-check", "status": "error", "error": "no Routes API key",
                "checked": [], "alerts": 0}

    lookahead = timedelta(hours=traffic.lookahead_hours)
    events = fetch_events_by_person(
        config.calendar, time_min=now, time_max=now + lookahead
    )
    legs = rules.upcoming_commutes(
        events,
        home_address=config.family.home_address,
        now=now,
        lookahead=lookahead,
        origin_lookback_min=traffic.origin_lookback_min,
    )
    recent = dedup.recent_keys(traffic.dedup_window_min, now=now)

    checked: list[dict[str, Any]] = []
    alerts = 0
    for leg in legs:
        key = rules.dedup_key(leg.person, leg.event.summary)
        try:
            result = compute_route(
                leg.origin, leg.destination, api_key=traffic.api_key,
                arrival_time=leg.event.start,
            )
        except TrafficReadError as exc:
            checked.append({
                "person": leg.person, "event": leg.event.summary,
                "status": "error", "detail": str(exc), "dedup_key": key,
                "checked_at": now.isoformat(),
            })
            continue
        status = delay_status(result.delay_min, significant_min=traffic.significant_delay_min)
        entry = {
            "person": leg.person, "event": leg.event.summary,
            "origin": leg.origin, "destination": leg.destination,
            "normal_min": result.normal_s // 60, "traffic_min": result.traffic_s // 60,
            "delay_min": result.delay_min, "status": status,
            "dedup_key": key, "alerted": False, "checked_at": now.isoformat(),
        }
        if status == "SIGNIFICANT_DELAY" and key not in recent:
            if not dry_run:
                text = _alert_text(leg.person, leg.event.summary, result, result.delay_min)
                send_alert(config, text)
                dedup.record_alert(key, now=now)
                recent.add(key)
            entry["alerted"] = True
            alerts += 1
        checked.append(entry)

    return {
        "kind": "traffic-check", "status": "ok",
        "checked": checked, "alerts": alerts, "dry_run": dry_run,
    }
