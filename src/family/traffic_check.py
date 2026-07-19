"""Traffic-jam insurance check (issues #160, #169) — deterministic, one-shot.

Pipeline: quiet-hours gate → fetch upcoming events → classify commutes →
resolve each leg's origin → one Routes call per leg → dedup → alert only on a
significant delay or an infeasible back-to-back hop. Returns a schema-stable
result payload (one entry per route checked, always-present ``dedup_key``, one
timestamp format, real API output only) that the CLI persists as the run's
``summary_json`` (#163) and prints.

Origin resolution (#169): the responsible person's *live phone position* when
home-automation reports a fresh fix, else the calendar-inference chain (home, or
a preceding back-to-back commute's destination) — recorded per leg as
``location_source`` so the Audit trace shows which was used for every decision.
Privacy: raw coordinates are used only to build the outbound Routes request and
are **never** written into the payload — the trace carries a label plus derived
values (freshness age, delay/ETA minutes), never lat/lon.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from src.config import Config
from src.family import dedup, rules
from src.family.calendar_source import fetch_events_by_person
from src.notify.alert import send_alert
from src.presence import PresenceLocation, get_location
from src.traffic import RouteResult, TrafficReadError, compute_route, delay_status

_LIVE_PRESENCE = "live_presence"
_CALENDAR_INFERENCE = "calendar_inference"


def _alert_text(person: str, leg_summary: str, result: RouteResult, delay: int) -> str:
    return (
        f"🚗 Traffic alert — {person}: “{leg_summary}”. "
        f"Now ~{result.traffic_s // 60} min vs {result.normal_s // 60} min normal "
        f"(+{delay} min). Leave earlier."
    )


def _infeasible_text(person: str, leg_summary: str, travel_min: int, gap_min: int) -> str:
    return (
        f"⛔ Tight schedule — {person}: “{leg_summary}”. "
        f"Only {gap_min} min between events but the drive is ~{travel_min} min. "
        f"They may not make it on time."
    )


def _resolve_origin_for_leg(
    config: Config, leg: rules.CommuteLeg, *, now: datetime
) -> dict[str, Any]:
    """Pick the routing origin for one leg: live phone fix, else calendar chain.

    Returns the routing inputs plus the privacy-safe trace fields — ``origin`` is
    a label/address (never coordinates) and ``origin_latlng`` (the raw fix) stays
    out of any persisted structure.
    """
    location = get_location(config.presence, leg.person, now=now)
    if isinstance(location, PresenceLocation):
        return {
            "origin_latlng": (location.latitude, location.longitude),
            "origin_label": "live phone position",
            "location_source": _LIVE_PRESENCE,
            "presence_age_min": location.age_min,
            "presence_refreshed": location.refreshed,
            "presence_status": None,
        }
    return {
        "origin_latlng": None,
        "origin_label": leg.origin,
        "location_source": _CALENDAR_INFERENCE,
        "presence_age_min": None,
        "presence_refreshed": False,
        "presence_status": location.reason,
    }


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
        origin = _resolve_origin_for_leg(config, leg, now=now)
        try:
            result = compute_route(
                origin["origin_label"], leg.destination, api_key=traffic.api_key,
                arrival_time=leg.event.start, origin_latlng=origin["origin_latlng"],
            )
        except TrafficReadError as exc:
            checked.append({
                "person": leg.person, "event": leg.event.summary,
                "status": "error", "detail": str(exc), "dedup_key": key,
                "location_source": origin["location_source"],
                "presence_status": origin["presence_status"],
                "checked_at": now.isoformat(),
            })
            continue
        status = delay_status(result.delay_min, significant_min=traffic.significant_delay_min)

        # Back-to-back adjacency feasibility (#169, completing #168's deferral):
        # only meaningful for a calendar-chained origin, where the gap between the
        # preceding event's end and this event's start is a real departure budget.
        # A live-presence origin has no such fixed departure moment, so it is not
        # feasibility-judged — only its delay is.
        gap_min: int | None = None
        feasible: bool | None = None
        if (
            origin["location_source"] == _CALENDAR_INFERENCE
            and leg.origin_event_end is not None
        ):
            gap_min = int((leg.event.start - leg.origin_event_end).total_seconds() // 60)
            feasible = (result.traffic_s / 60.0) <= gap_min

        entry = {
            "person": leg.person, "event": leg.event.summary,
            "origin": origin["origin_label"], "destination": leg.destination,
            "location_source": origin["location_source"],
            "presence_age_min": origin["presence_age_min"],
            "presence_refreshed": origin["presence_refreshed"],
            "presence_status": origin["presence_status"],
            "normal_min": result.normal_s // 60, "traffic_min": result.traffic_s // 60,
            "delay_min": result.delay_min, "status": status,
            "gap_min": gap_min, "feasible": feasible,
            "dedup_key": key, "alerted": False, "checked_at": now.isoformat(),
        }
        alert_needed = status == "SIGNIFICANT_DELAY" or feasible is False
        if alert_needed and key not in recent:
            if not dry_run:
                if feasible is False and gap_min is not None:
                    text = _infeasible_text(
                        leg.person, leg.event.summary, result.traffic_s // 60, gap_min
                    )
                else:
                    text = _alert_text(
                        leg.person, leg.event.summary, result, result.delay_min
                    )
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
