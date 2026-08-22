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
triggers it — no real position, no claim about where the person is. When more
than one person's leave-now lands on the same event (#213), they are combined
into one message naming everyone instead of one redundant message per person.
Grouping is by event alone — summary and start. The ETA deliberately is *not*
part of that key: it is derived from each person's own phone fix, so including
it (as #213 originally did) split two people sharing one car into separate
messages over a one-minute difference. The merged text quotes the group's
largest ETA, since the nudge has to be right for whoever is furthest out (#252).

A commute whose title marks it as taken by train (#227) is exempt from *both*
driving-ETA judgments while ``traffic.skip_leave_now_for_train`` is on: the
leave-now nudge and the infeasible-hop "tight schedule" alert both reason from
a Routes DRIVE result, which says nothing about a train departure (#252).
Recorded per leg as ``leave_now_suppressed`` / ``infeasible_suppressed``. The
*delay* alert stays untouched — congestion on the road is a real-world signal
regardless of who is driving.

Departure anchors (#270): every leg is priced for the moment its driver is
expected to *set off*, because ``departureTime`` is the only time field Routes
v2 honours for a ``DRIVE`` route — an ``arrivalTime`` request is accepted and
then ignored, returning the depart-now baseline. This check passed
``arrival_time`` from #160 until #270, so every alert above was in fact judging
a 09:00 school run against whatever traffic existed at the 07:00 sweep. Which
moment each leg is anchored to is decided in :func:`_departure_anchor` and
recorded per leg as ``anchor`` / ``departure_anchor``. A departure moment that
has already passed is never *sent* (Routes rejects it outright), but it is only
reported as the ``anchor_in_the_past`` state when the thing being judged is over
— the event the leg travels *to* has started. A driver whose previous event
ended ten minutes ago has not left yet, so their honest anchor is ``now``, and
the tight-schedule warning they are about to need still fires.

Origin resolution (#169): the responsible person's *live phone position* when
home-automation reports a fresh fix, else the calendar-inference chain (home, or
a preceding back-to-back commute's destination) — recorded per leg as
``location_source`` so the Audit trace shows which was used for every decision.
Privacy: raw coordinates are used only to build the outbound Routes request and
are **never** written into the payload — the trace carries a label plus derived
values (freshness age, delay/ETA minutes), never lat/lon.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import requests

from src.config import Config
from src.family import dedup, rules
from src.family.calendar_source import fetch_events_by_person
from src.notify.alert import send_alert
from src.presence import PresenceLocation, get_location
from src.traffic import RouteResult, TrafficReadError, compute_route, delay_status

logger = logging.getLogger(__name__)

_LIVE_PRESENCE = "live_presence"
_CALENDAR_INFERENCE = "calendar_inference"

#: Which departure moment a leg was priced for (#270), carried on every checked
#: leg so the Audit trace shows the anchor behind the number, not just the number.
ANCHOR_DEPART_NOW = "depart_now"
#: Depart-now, reached the other way: a chained hop whose preceding event has
#: already ended while the driver is still sitting there. Distinguished from
#: :data:`ANCHOR_DEPART_NOW` in the trace because the two answer different
#: questions — "where are they right now" vs "they are already late leaving".
ANCHOR_DEPART_NOW_OVERDUE = "depart_now_overdue"
ANCHOR_PRECEDING_EVENT_END = "preceding_event_end"
ANCHOR_EVENT_START = "event_start"

#: The leg is over: the event it travels *to* has already started, so there is no
#: departure left to price. Reported as its own state, never as a routes error,
#: and priced with no Routes call at all — Routes rejects a past ``departureTime``
#: with ``HTTP 400 "Timestamp must be set to a future time."``, and both alerts it
#: would have fed ("leave earlier", "you may not make it") are advice about a
#: drive that is already decided. Mirrors ``travel_blocks.FAILURE_ANCHOR_IN_THE_PAST``.
#:
#: A merely *elapsed* anchor is deliberately not this state: a driver whose
#: previous event ended is late, not finished, and #282's first cut wrongly went
#: silent on exactly the tight-schedule window where the warning matters most.
#: :func:`_departure_anchor` folds that case to depart-now instead, so this state
#: is reached only through the guard in :func:`run_traffic_check` — which
#: ``upcoming_commutes`` already makes a boundary case (it admits no leg whose
#: event has started). It is kept as a hard guarantee that no past timestamp can
#: reach the wire however a future anchor comes to be derived.
STATUS_ANCHOR_IN_THE_PAST = "anchor_in_the_past"


def _alert_text(
    person: str, leg_summary: str, result: RouteResult, delay: int,
    *, anchor: datetime | None,
) -> str:
    """The delay alert, stamped with the moment it was priced for (#270).

    Before #270 this always opened with "Now", which was true of the estimate
    (Routes silently priced every leg depart-now) but not of the claim. Now that
    a leg can genuinely be priced for a future departure the message has to say
    which: "Now ~40 min" about a 17:30 drive is the same silent wrongness in
    words instead of in a request body.
    """
    when = "Now" if anchor is None else f"At {anchor.strftime('%H:%M')}"
    return (
        f"🚗 Traffic alert — {person}: “{leg_summary}”. "
        f"{when} ~{result.traffic_s // 60} min vs {result.normal_s // 60} min normal "
        f"(+{delay} min). Leave earlier."
    )


def _infeasible_text(person: str, leg_summary: str, travel_min: int, gap_min: int) -> str:
    return (
        f"⛔ Tight schedule — {person}: “{leg_summary}”. "
        f"Only {gap_min} min between events but the drive is ~{travel_min} min. "
        f"They may not make it on time."
    )


def _leave_now_text(
    people: list[str], leg_summary: str, eta_min: int, event_start: datetime
) -> str:
    return (
        f"🚗 Leave now — {' and '.join(people)}: “{leg_summary}”. "
        f"Drive is ~{eta_min} min with traffic; it starts at "
        f"{event_start.strftime('%H:%M')}."
    )


def _resolve_origin_for_leg(
    config: Config, leg: rules.CommuteLeg, *, now: datetime, session: requests.Session
) -> dict[str, Any]:
    """Pick the routing origin for one leg: live phone fix, else calendar chain.

    Returns the routing inputs plus the privacy-safe trace fields — ``origin`` is
    a label/address (never coordinates) and ``origin_latlng`` (the raw fix) stays
    out of any persisted structure.
    """
    location = get_location(config.presence, leg.person, now=now, session=session)
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


def _departure_anchor(
    leg: rules.CommuteLeg, location_source: str, *, now: datetime
) -> tuple[datetime | None, str]:
    """The moment this leg must be priced as a *departure*, and why (#270).

    Routes v2 honours exactly one time field for ``DRIVE`` + ``TRAFFIC_AWARE``:
    ``departureTime``. ``arrivalTime`` is accepted and then ignored, returning
    the depart-now baseline (probe table in #270), so the anchor is not a
    cosmetic choice — it *is* the estimate. ``None`` means *send no time field*,
    which is Routes' own "depart now" and is a deliberate anchor, not a missing
    one.

    One Routes call prices one leg, and each leg carries exactly one judgment
    that needs a departure moment, so one anchor per leg is enough:

    * **Live phone fix** → ``None`` (depart now). The judgment this leg exists
      for is the leave-now nudge (#185), which asks "if they set off *now*, do
      they still make it?" — that is a departure at ``now``, and sending no time
      field is how Routes is asked for exactly that. The delay alert riding the
      same call therefore reads the traffic on the road right now, which is the
      right frame for someone being told whether to move. Deliberately *not*
      anchored to the event start: that would make ``depart_in_min`` an estimate
      for a drive starting hours later, and the nudge is the actionable half.
    * **Chained off a preceding event** → that event's ``end``, or ``None``
      (depart now) once that end has passed. The judgment is the infeasible-hop
      check (#169, #252): can the gap between two events absorb the drive? The
      person sets off when the first event ends, so that is the departure being
      priced. Once it has passed they have *not* left — they are late — and the
      honest anchor is ``now``: an office event ending at 13:00, lunch elsewhere
      at 13:15 and a sweep at 13:10 is precisely the window where a 25-minute
      drive most needs the "you may not make it" warning, and reporting that leg
      as unpriceable silently dropped it (review of #282). ``gap_min`` is
      measured from the preceding event's end either way, so the feasibility
      verdict is unaffected by which of the two anchors priced the drive.
    * **From home, no live fix** → the event's ``start``. Only the delay alert
      applies, and the true departure is one drive-length *earlier* — which is
      the number the call is about to return, so it cannot be known first. Same
      trade-off ``travel_blocks._departure_anchor`` already reasons out for an
      outbound leg: wrong by at most the drive's own length (10-30 min) against
      being wrong by the whole sweep-to-event distance (hours) if the moment
      were effectively dropped, and refining it would cost a second call per leg.
    """
    if location_source == _LIVE_PRESENCE:
        return None, ANCHOR_DEPART_NOW
    if leg.origin_event_end is not None:
        if leg.origin_event_end <= now:
            # Not stale — late. Depart-now is expressed as "no time field" rather
            # than a literal `now`, which would race the API's own clock into the
            # past that Routes rejects.
            return None, ANCHOR_DEPART_NOW_OVERDUE
        return leg.origin_event_end, ANCHOR_PRECEDING_EVENT_END
    return leg.event.start, ANCHOR_EVENT_START


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
    # Never shorter than the lookahead (#252) — see `effective_dedup_window_min`.
    # The window actually applied rides the run payload below, so a
    # configured-but-overridden value is visible in the Audit tab: this repo
    # configures no logging handlers, so the log line alone would be dropped.
    dedup_window_min = traffic.effective_dedup_window_min
    if dedup_window_min != traffic.dedup_window_min:
        logger.info(
            "ℹ️ dedup_window_min=%d is shorter than the %dh lookahead; using %d "
            "so one event alerts once",
            traffic.dedup_window_min, traffic.lookahead_hours, dedup_window_min,
        )
    recent = dedup.recent_keys(dedup_window_min, now=now)

    checked: list[dict[str, Any]] = []
    alerts = 0
    # Leave-now candidates ready to fire, grouped by the parts of the message
    # that would otherwise be identical (#213) — merged into one send per group
    # once every leg has been checked.
    pending_leave_now: dict[tuple[str, datetime], list[dict[str, Any]]] = {}
    with requests.Session() as session:
        for leg in legs:
            key = rules.dedup_key(leg.person, leg.event.summary)
            origin = _resolve_origin_for_leg(config, leg, now=now, session=session)
            anchor, anchor_kind = _departure_anchor(
                leg, origin["location_source"], now=now
            )
            anchor_iso = anchor.isoformat() if anchor is not None else None
            if anchor is not None and anchor <= now:
                # Reached only for a leg whose own event has already started —
                # `_departure_anchor` folds a merely-late departure to depart-now,
                # and `upcoming_commutes` admits no leg whose event has begun, so
                # this is the boundary guard that keeps a past timestamp off the
                # wire (a hard HTTP 400) no matter how the anchor was derived.
                # No call, and no error either: nothing failed, the drive is over.
                checked.append({
                    "person": leg.person, "event": leg.event.summary,
                    "status": STATUS_ANCHOR_IN_THE_PAST,
                    "detail": "departure moment has already passed; "
                              "Routes prices only future departures",
                    "dedup_key": key,
                    "location_source": origin["location_source"],
                    "presence_status": origin["presence_status"],
                    "anchor": anchor_kind, "departure_anchor": anchor_iso,
                    "checked_at": now.isoformat(),
                })
                continue
            try:
                result = compute_route(
                    origin["origin_label"], leg.destination, api_key=traffic.api_key,
                    departure_time=anchor, origin_latlng=origin["origin_latlng"],
                    session=session,
                )
            except TrafficReadError as exc:
                checked.append({
                    "person": leg.person, "event": leg.event.summary,
                    "status": "error", "detail": str(exc), "dedup_key": key,
                    "location_source": origin["location_source"],
                    "presence_status": origin["presence_status"],
                    "anchor": anchor_kind, "departure_anchor": anchor_iso,
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
            # A train-titled commute (#227) is exempt: the ETA above is a *driving*
            # ETA, so its departure moment is meaningless for a train ride. The
            # computed `depart_in` is still recorded — it stays informative — and the
            # suppression is recorded explicitly rather than silently dropping the
            # alert. The *delay* alert is deliberately unaffected.
            #
            # Both driving-ETA judgments below share this one train check (#252):
            # a "the drive is ~10 min" claim is meaningless for a train ride,
            # whether it is phrased as leave-now or as an infeasible hop.
            skip_driving = traffic.skip_leave_now_for_train and rules.is_train_commute(
                leg.event, traffic.train_keywords
            )

            depart_in: int | None = None
            leave_now = False
            leave_now_suppressed = False
            if origin["location_source"] == _LIVE_PRESENCE:
                depart_in = rules.depart_in_min(
                    now, result.traffic_s // 60, leg.event.start, traffic.leave_margin_min
                )
                leave_now = depart_in <= 0
                if leave_now and skip_driving:
                    leave_now = False
                    leave_now_suppressed = True

            # The infeasible-hop alert is the same driving claim in different
            # words, so it takes the same exemption (#252). Recorded explicitly
            # rather than silently dropped, mirroring `leave_now_suppressed`.
            infeasible_alert = feasible is False
            infeasible_suppressed = False
            if infeasible_alert and skip_driving:
                infeasible_alert = False
                infeasible_suppressed = True

            entry = {
                "person": leg.person, "event": leg.event.summary,
                "origin": origin["origin_label"], "destination": leg.destination,
                "location_source": origin["location_source"],
                "presence_age_min": origin["presence_age_min"],
                "presence_refreshed": origin["presence_refreshed"],
                "presence_status": origin["presence_status"],
                "normal_min": result.normal_s // 60, "traffic_min": result.traffic_s // 60,
                "delay_min": result.delay_min, "status": status,
                "anchor": anchor_kind, "departure_anchor": anchor_iso,
                "gap_min": gap_min, "feasible": feasible,
                "depart_in_min": depart_in, "leave_margin_min": traffic.leave_margin_min,
                "dedup_key": key, "alerted": False, "leave_now_alerted": False,
                "leave_now_suppressed": leave_now_suppressed,
                "infeasible_suppressed": infeasible_suppressed,
                "checked_at": now.isoformat(),
            }
            alert_needed = status == "SIGNIFICANT_DELAY" or infeasible_alert
            if alert_needed and key not in recent:
                if not dry_run:
                    if infeasible_alert and gap_min is not None:
                        text = _infeasible_text(
                            leg.person, leg.event.summary, result.traffic_s // 60, gap_min
                        )
                    else:
                        text = _alert_text(
                            leg.person, leg.event.summary, result, result.delay_min,
                            anchor=anchor,
                        )
                    send_alert(config, text)
                    dedup.record_alert(key, now=now)
                    recent.add(key)
                entry["alerted"] = True
                alerts += 1

            # A distinct dedup key lets a leave-now alert coexist with a delay alert
            # for the same event without either suppressing the other. Sending is
            # deferred until every leg is checked, so people sharing one event with
            # identical resulting text (#213) can be merged into one message.
            leave_key = rules.leave_now_dedup_key(leg.person, leg.event.summary)
            if leave_now and leave_key not in recent:
                # Group by the event alone (#252). The ETA used to be part of
                # this key, but it is computed per person from their own live
                # phone fix, so two people heading to the same event almost
                # never matched — "~1 min" vs "~0 min" for two people in one
                # car produced two identical-looking messages, which is exactly
                # what #213's merge was meant to prevent.
                group_key = (leg.event.summary, leg.event.start)
                pending_leave_now.setdefault(group_key, []).append(
                    {
                        "person": leg.person,
                        "leave_key": leave_key,
                        "eta_min": result.traffic_s // 60,
                        "entry": entry,
                    }
                )
            checked.append(entry)

    for (summary, event_start), items in pending_leave_now.items():
        if not dry_run:
            people = [item["person"] for item in items]
            # The largest ETA in the group: the nudge has to be right for
            # whoever is furthest out, or it tells the slowest person they have
            # more time than they do.
            eta_min = max(item["eta_min"] for item in items)
            send_alert(config, _leave_now_text(people, summary, eta_min, event_start))
            for item in items:
                dedup.record_alert(item["leave_key"], now=now)
                recent.add(item["leave_key"])
        for item in items:
            item["entry"]["leave_now_alerted"] = True
        alerts += 1

    return {
        "kind": "traffic-check", "status": "ok",
        "checked": checked, "alerts": alerts, "dry_run": dry_run,
        # The window actually applied, which may be higher than configured (#252).
        "dedup_window_min": dedup_window_min,
    }
