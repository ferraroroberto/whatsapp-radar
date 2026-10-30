"""Daily calendar-conflict scan — deterministic, one-shot.

Pipeline: fetch the next few days for both calendars → per-event decision trace
(every event gets a recorded location verdict with its reason; a missing
location is *assumed home* and flagged, never silently dropped) → apply the
fixed weekly responsibility pattern plus two-places-at-once detection over the
assessment window → live phone-position ETA judgment for today's imminent
windows when presence is enabled (additive signal, calendar inference
stays authoritative for intent and for anything beyond the lookahead) → a priced
commute travel-block plan reconciled against the blocks already on the
calendars, and (only when explicitly enabled *and* taken out of dry run)
applied to them (off and dry-run by default, reusing the same fetched
events) → always
send one summary on a live run: coverage issues and missing-location asks, or
an explicit all-clear. Coverage gaps and overlaps are
hard alerts and bypass quiet hours; a clean summary inside quiet hours is
suppressed until the next daytime run. Returns a schema-stable result payload
the CLI persists as the run's ``summary_json`` and prints.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

from src.config import Config
from src.family import rules
from src.family.calendar_source import fetch_events_by_person
from src.family.travel_blocks import scan_window_days
from src.family.travel_blocks_write import run_travel_blocks
from src.notify.alert import send_alert
from src.presence import PresenceLocation, get_location
from src.traffic import TrafficReadError, compute_route


def _day_bounds(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    # Built from `day` + `zone` directly, never `start + timedelta(days=1)`:
    # with a real (DST-aware) zone, the day after a DST change is 23 or 25
    # real hours long, so the next midnight isn't exactly 24h after this one.
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start, end


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
    ask_missing_locations: bool = True,
) -> str:
    """Compose the daily Telegram summary — always produced, even all-clear.

    Per-person coverage issues come first (they are the hard alerts), then the
    missing-location asks grouped by person, then the explicit all-clear when
    there is nothing at all — silence is never a result.

    ``ask_missing_locations=False`` drops the asks section for a
    household where some events will never get a location, so the recurring
    nag stops drowning the coverage alerts it shares a message with. It changes
    only this text: ``missing`` is still computed and still persisted in the
    run payload. With the asks hidden and no conflicts, the message correctly
    degrades to the all-clear rather than to an empty header.
    """
    asks = missing if ask_missing_locations else []
    lines = [f"📅 Calendar sync — checked the next {scan_days} days."]
    if conflicts:
        lines.append(f"⚠️ {len(conflicts)} issue(s) in the next {assessment_days} day(s):")
        lines.extend(f"• {c['detail']}" for c in conflicts[:6])
        if len(conflicts) > 6:
            lines.append(f"…and {len(conflicts) - 6} more.")
    if asks:
        lines.append("📍 No location set — please add one:")
        by_person: dict[str, list[str]] = {}
        # Sorted by (person, start) so the same inputs always compose the same
        # message regardless of fetch order — determinism is part of the spec.
        for item in sorted(asks, key=lambda m: (str(m["person"]), str(m["start"]))):
            by_person.setdefault(item["person"], []).append(
                f"{_fmt_when(item['start'])} — {item['event']}"
            )
        for person, items in sorted(by_person.items()):
            for entry in items[:4]:
                lines.append(f"• {person}: {entry}")
            if len(items) > 4:
                lines.append(f"• {person}: …and {len(items) - 4} more.")
    if not conflicts and not asks:
        lines.append("✅ Everything is fine — nothing needs your attention.")
    return "\n".join(lines)


def _assess_live_coverage(
    config: Config, *, now: datetime
) -> tuple[list[rules.Conflict], list[dict[str, Any]]]:
    """Judge today's imminent childcare windows by phone → home ETA.

    Only windows starting within the traffic lookahead are judged — a phone fix
    *now* says nothing about tomorrow. The verdict is additive: it never
    suppresses a calendar-based coverage gap, because position and calendar
    answer different questions (where the parent *is* vs. what they *intend* —
    a parent reachable-in-time right now may still have a commitment running
    through the window). Trace entries carry only derived values (age, distance,
    ETA, margin) — never coordinates.
    """
    family, traffic, presence_cfg = config.family, config.traffic, config.presence
    if not presence_cfg.enabled:
        return [], []
    day = now.date()
    responsible = family.responsible_by_weekday.get(day.weekday())
    if not responsible:
        return [], []
    tz = now.tzinfo or UTC
    horizon = now + timedelta(hours=traffic.lookahead_hours)
    imminent = [
        (label, datetime.combine(day, start, tzinfo=tz))
        for label, start, _end in rules.day_windows(family, day)
        if now < datetime.combine(day, start, tzinfo=tz) <= horizon
    ]
    if not imminent:
        return [], []

    location = get_location(presence_cfg, responsible, now=now)
    if not isinstance(location, PresenceLocation):
        return [], [{
            "person": responsible, "location_source": "calendar_inference",
            "presence_status": location.reason, "assessed": False,
            "windows": [label for label, _ in imminent],
        }]

    base: dict[str, Any] = {
        "person": responsible, "location_source": "live_presence",
        "presence_age_min": location.age_min,
        "presence_refreshed": location.refreshed,
        "at_home": location.at_home,
        "distance_from_home_km": location.distance_from_home_km,
        "assessed": True,
    }
    if location.at_home:
        return [], [
            {**base, "window": label, "start": start.isoformat(),
             "eta_min": 0, "margin_min": rules.arrival_margin_min(now, 0, start),
             "feasible": True}
            for label, start in imminent
        ]

    if not traffic.api_key:
        return [], [{**base, "assessed": False, "presence_status": "no_routes_api_key",
                     "windows": [label for label, _ in imminent]}]
    try:
        # One route serves every window: same origin (the phone) and the same
        # destination (home, where childcare happens) — only the margins differ.
        route = compute_route(
            "live phone position", family.home_address,
            api_key=traffic.api_key, origin_latlng=(location.latitude, location.longitude),
        )
    except TrafficReadError as exc:
        return [], [{**base, "assessed": False, "presence_status": f"routes_error: {exc}",
                     "windows": [label for label, _ in imminent]}]

    eta_min = route.traffic_s / 60.0
    conflicts: list[rules.Conflict] = []
    coverage: list[dict[str, Any]] = []
    for label, start in imminent:
        margin = rules.arrival_margin_min(now, eta_min, start)
        feasible = margin >= 0
        coverage.append({**base, "window": label, "start": start.isoformat(),
                         "eta_min": round(eta_min), "margin_min": margin,
                         "feasible": feasible})
        if not feasible:
            conflicts.append(rules.Conflict(
                kind="coverage_eta", day=day.isoformat(),
                detail=(
                    f"{responsible} is ~{round(eta_min)} min from home but "
                    f"'{label}' starts at {start.strftime('%H:%M')} — "
                    f"{-margin} min short even leaving now"
                ),
            ))
    return conflicts, coverage


def _calendar_conflicts(
    config: Config,
    events: dict[str, list[Any]],
    *,
    now: datetime,
    midnight: datetime,
) -> list[rules.Conflict]:
    """Hard conflicts and overlaps across every assessed day."""
    family = config.family
    # The zone every day below is judged in — a real ZoneInfo when
    # `family.timezone` is configured, so each day re-resolves its own correct
    # offset across a DST change instead of freezing `now`'s; otherwise the
    # fallback is `now.tzinfo` (correct for `now` itself).
    zone = rules.household_zone(family.timezone, now)

    conflicts: list[rules.Conflict] = []
    for offset in range(family.assessment_days):
        # Walk the *date* off `midnight`'s — not `midnight + timedelta`'s
        # frozen offset — and re-anchor it in `zone` for each day via
        # `_day_bounds`/`find_conflicts`'s own `datetime.combine(day, t,
        # tzinfo=zone)`, so this cannot drift from the fetch window above
        # yet still lands on the correct wall-clock offset per day.
        day = midnight.date() + timedelta(days=offset)
        day_min, day_max = _day_bounds(day, zone)
        day_events = {
            person: [e for e in evs if day_min <= e.start < day_max]
            for person, evs in events.items()
        }
        conflicts.extend(
            rules.find_conflicts(day_events, family, day=day, tz=zone)
        )
        conflicts.extend(
            rules.find_overlaps(day_events, home_address=family.home_address)
        )
    return conflicts


def calendar_run_hour_skip_reason(config: Config, *, now: datetime) -> str | None:
    """Hour self-skip — the reason to skip an unforced ``calendar-scan``, or None.

    ``family.run_hour`` is exposed and editable on ``/api/family``, so this gate
    is what makes the knob mean what it says — a scan fired before ``run_hour``
    local time does nothing and says so.

    Deliberately mirrors :func:`src.family.traffic_check.traffic_cadence_skip_reason`:
    the external job is armed generously and the *effective* schedule follows
    the config, so an edit in the Family tab takes effect on the next fire with
    no App Launcher re-arm. Like that one it is checked before the caller opens
    a run row, so a skip writes **no DB run row** — which is also what keeps it
    out of the Family tab's "last sweep" lookup, a scan of ``review_runs`` for
    the newest ``calendar-scan`` payload carrying a ``travel_blocks`` section
    (``app/webapp/routers/family.py::_last_travel_sweep``). A skip that
    recorded a row would either be mistaken for a completed sweep or blank out
    the genuine earlier one. The caller's filesystem run record is still
    written and reads as ``skipped``, so the fire is auditable without
    polluting the history the Family tab reasons over.

    Callers must not invoke this for a **forced** fire: ``--force`` is how an
    explicit, human-requested run (every button in the webapp, and a hand-typed
    terminal run) says "I am asking for this now", and the whole point of an
    hour gate is that it governs the unattended schedule, not the operator.

    The comparison is on the local hour only — no "already ran today" memory.
    ``run_hour`` answers "not before when", which is all the knob ever claimed;
    once-a-day is the job's business, and the reconcile is idempotent anyway.
    """
    run_hour = config.family.run_hour
    if now.hour >= run_hour:
        return None
    return (
        f"before family.run_hour={run_hour:02d}:00 "
        f"(local hour {now.hour:02d}); pass --force to run anyway"
    )


def run_calendar_scan(config: Config, *, now: datetime, dry_run: bool) -> dict[str, Any]:
    """Run one calendar sync. ``dry_run`` never sends anything."""
    family = config.family
    if not family.enabled and not dry_run:
        return {"kind": "calendar-scan", "status": "disabled", "conflicts": [],
                "missing_locations": [], "decisions": [],
                "summary": {"status": "skipped", "reason": "disabled"}}

    # One fetch over the full missing-location window covers the assessment days.
    midnight = rules.local_midnight(now)
    scan_days = scan_window_days(config)
    window_end = midnight + timedelta(days=scan_days)
    events = fetch_events_by_person(config.calendar, time_min=midnight, time_max=window_end)

    decisions = rules.event_decisions(events, home_address=family.home_address)
    missing = [
        {"person": person, "event": event.summary, "start": event.start.isoformat()}
        for person, event in rules.find_missing_locations(
            events, home_address=family.home_address
        )
    ]

    conflicts = _calendar_conflicts(config, events, now=now, midnight=midnight)

    # Commute travel blocks — reuses the events fetched
    # above rather than re-reading them (the reconcile's own marker-scoped read
    # is a separate, narrower query). Off by default, and dry-run by default even
    # once enabled: a dry run computes and logs the whole add/delete plan and
    # touches no calendar. Never raises — a write failure is a recorded status.
    #
    # `force_dry_run=dry_run`: this verb's own --dry-run must mean "this
    # invocation changes nothing outside the process", not merely "send no
    # alert". Without it a `calendar-scan --dry-run` on a live-configured
    # install would insert and delete real calendar events — and the Family
    # tab's rehearse button would be a write dressed as a rehearsal.
    travel_block_plan = run_travel_blocks(config, events, now=now, force_dry_run=dry_run)

    # Live phone-position judgment for today's imminent windows —
    # additive to the calendar-based gaps, never a replacement for them.
    live_conflicts, live_coverage = _assess_live_coverage(config, now=now)
    conflicts.extend(live_conflicts)

    conflict_dicts = [{"kind": c.kind, "day": c.day, "detail": c.detail} for c in conflicts]
    text = build_summary_text(
        scan_days=scan_days,
        assessment_days=family.assessment_days,
        conflicts=conflict_dicts,
        missing=missing,
        ask_missing_locations=family.ask_missing_locations,
    )

    # Always-send contract: a live run produces exactly one summary —
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
        "live_coverage": live_coverage,
        "summary": summary,
        "dry_run": dry_run,
        # Additive: every key above keeps its shape when this one is added.
        "travel_blocks": travel_block_plan,
    }
