"""Traffic-jam insurance check (issues #160, #169, #185) — deterministic, one-shot.

Pipeline: quiet-hours gate → fetch upcoming events → classify commutes →
resolve each leg's origin → one Routes call per leg → dedup → alert on a
significant delay, an infeasible back-to-back hop, or the moment a live-tracked
person must leave to make an event on time (#185). Returns a schema-stable
result payload (one entry per route checked, always-present ``dedup_key``, one
timestamp format, real API output only) that the CLI persists as the run's
``summary_json`` (#163) and prints.

The leave-now alert (#185) closes the loop from *detection* to *action*: when a
live phone fix puts the person far enough out that ``event.start - (now + eta +
leave_margin_min) <= 0``, one Telegram nudge fires, deduped independently of the
delay alert so both can coexist for one event. A calendar-inference origin never
triggers it — no real position, no claim about where the person is.

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


def _leave_now_text(
    person: str, leg_summary: str, eta_min: int, event_start: datetime
) -> str:
    return (
        f"🚗 Leave now — {person}: “{leg_summary}”. "
        f"Drive is ~{eta_min} min with traffic; it starts at "
        f"{event_start.strftime('%H:%M')}."
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

        # Leave-now judgment (#185): the loop from detection to action. Only a
        # live phone fix supports it — a calendar-inference origin makes no claim
        # about where the person actually is, so it never triggers a leave-now.
        # Timeliness is bounded by the check cadence (#170): the alert lands on
        # the first fire after the departure moment, so `traffic.cadence_min`
        # should be low when relying on leave-now.
        depart_in: int | None = None
        leave_now = False
        if origin["location_source"] == _LIVE_PRESENCE:
            depart_in = rules.depart_in_min(
                now, result.traffic_s // 60, leg.event.start, traffic.leave_margin_min
            )
            leave_now = depart_in <= 0

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
            "depart_in_min": depart_in, "leave_margin_min": traffic.leave_margin_min,
            "dedup_key": key, "alerted": False, "leave_now_alerted": False,
            "checked_at": now.isoformat(),
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

        # A distinct dedup key lets a leave-now alert coexist with a delay alert
        # for the same event without either suppressing the other.
        leave_key = rules.leave_now_dedup_key(leg.person, leg.event.summary)
        if leave_now and leave_key not in recent:
            if not dry_run:
                send_alert(config, _leave_now_text(
                    leg.person, leg.event.summary, result.traffic_s // 60, leg.event.start
                ))
                dedup.record_alert(leave_key, now=now)
                recent.add(leave_key)
            entry["leave_now_alerted"] = True
            alerts += 1
        checked.append(entry)

    return {
        "kind": "traffic-check", "status": "ok",
        "checked": checked, "alerts": alerts, "dry_run": dry_run,
    }
