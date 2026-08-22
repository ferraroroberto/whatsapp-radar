"""Google Routes API v2 ``computeRoutes`` client (issue #160).

Thin, deterministic wrapper: one POST returning the free-flow (``staticDuration``)
and traffic-aware (``duration``) driving times between two addresses, plus the
delay in minutes and a coarse status. API-key auth via ``X-Goog-Api-Key``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

_ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"
_FIELD_MASK = "routes.duration,routes.staticDuration"
_TIMEOUT_S = 20

# Thresholds (minutes of delay vs. free-flow baseline). Only SIGNIFICANT alerts.
DELAY_THRESHOLD_MIN = 5
SIGNIFICANT_DELAY_THRESHOLD_MIN = 15


class TrafficReadError(RuntimeError):
    """A privacy-safe Routes failure suitable for logs and status surfaces."""


@dataclass(frozen=True)
class RouteResult:
    """Free-flow vs. traffic-aware driving time between two addresses."""

    normal_s: int
    traffic_s: int

    @property
    def delay_min(self) -> int:
        return max(0, round((self.traffic_s - self.normal_s) / 60))

    @property
    def status(self) -> str:
        return delay_status(self.delay_min)


def delay_status(
    delay_min: int,
    *,
    significant_min: int = SIGNIFICANT_DELAY_THRESHOLD_MIN,
    delay_min_threshold: int = DELAY_THRESHOLD_MIN,
) -> str:
    """Map a delay in minutes to NORMAL / DELAY / SIGNIFICANT_DELAY.

    ``significant_min`` is configurable per install (``traffic.significant_delay_min``)
    so only the alert threshold moves; the DELAY floor stays at the documented 5 min.
    """
    if delay_min > significant_min:
        return "SIGNIFICANT_DELAY"
    if delay_min >= delay_min_threshold:
        return "DELAY"
    return "NORMAL"


def _parse_seconds(value: Any) -> int:
    """Routes durations are strings like ``"742s"``; parse to int seconds."""
    text = str(value or "").strip().rstrip("s")
    if not text:
        raise ValueError("route is missing a duration")
    return int(float(text))


def compute_route(
    origin: str,
    destination: str,
    *,
    api_key: str,
    arrival_time: datetime | None = None,
    departure_time: datetime | None = None,
    origin_latlng: tuple[float, float] | None = None,
    session: requests.Session | None = None,
) -> RouteResult:
    """Compute the driving route ``origin`` → ``destination`` with live traffic.

    ``departure_time`` (aware datetime) requests a traffic estimate for leaving
    at that moment — the one time field Routes v2 actually honours for
    ``DRIVE`` + ``TRAFFIC_AWARE``, verified live against the API (#266): the
    same pair of addresses returns 983 s for a 04:00 departure and 930 s for an
    08:00 one, while every ``arrivalTime`` variant returns the depart-now
    baseline unchanged. It must be **in the future** — Routes rejects a past
    timestamp with ``HTTP 400 "Timestamp must be set to a future time."``, so
    callers pricing a schedule have to drop past anchors rather than send them.

    ``arrival_time`` is **refused** (#270). No caller in this repo passes it any
    more, and the parameter is kept only so that one that still does fails loudly
    instead of receiving the depart-now baseline dressed up as an arrival-window
    estimate — the exact silent wrongness that made #270 invisible for months.
    Deleting the parameter outright would raise ``TypeError`` from somewhere deep
    in a keyword-argument mismatch; raising :class:`TrafficReadError` here names
    the field to use instead, and keeps the "both fields" contract testable.

    ``origin_latlng`` (a ``(lat, lng)`` pair) routes from an exact position —
    the live phone fix (#169) — instead of the ``origin`` address string, which
    is then unused. Raises :class:`TrafficReadError` on any transport/API
    failure.
    """
    if not api_key:
        raise TrafficReadError("Routes API key is not configured")
    if arrival_time is not None and departure_time is not None:
        # Routes documents the two as mutually exclusive. It does not enforce it
        # (sending both returns 200 and honours the departure), so refusing here
        # is what stops a caller silently getting the other field's answer.
        raise TrafficReadError("arrival_time and departure_time are mutually exclusive")
    if arrival_time is not None:
        # Routes accepts `arrivalTime` for a DRIVE route and then ignores it,
        # returning the depart-now baseline (probe table in #270). An answer that
        # is real, plausible and not the question asked is worse than no answer,
        # so this never reaches the wire.
        raise TrafficReadError(
            "arrival_time is ignored by Routes for DRIVE; pass departure_time instead"
        )
    if origin_latlng is not None:
        origin_waypoint: dict[str, Any] = {
            "location": {"latLng": {"latitude": origin_latlng[0], "longitude": origin_latlng[1]}}
        }
    else:
        origin_waypoint = {"address": origin}
    body: dict[str, Any] = {
        "origin": origin_waypoint,
        "destination": {"address": destination},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    if departure_time is not None:
        # Routes requires an RFC-3339 timestamp; a local UTC offset is accepted.
        body["departureTime"] = departure_time.astimezone().isoformat()
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _FIELD_MASK,
    }
    http = session or requests
    try:
        response = http.post(_ENDPOINT, json=body, headers=headers, timeout=_TIMEOUT_S)
    except requests.RequestException as exc:
        raise TrafficReadError(f"Routes request failed ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise TrafficReadError(f"Routes API returned HTTP {response.status_code}")
    payload = response.json()
    routes = payload.get("routes") or []
    if not routes:
        raise TrafficReadError("Routes API returned no route for this origin/destination")
    route = routes[0]
    try:
        traffic_s = _parse_seconds(route.get("duration"))
        normal_s = _parse_seconds(route.get("staticDuration") or route.get("duration"))
    except ValueError as exc:
        raise TrafficReadError(str(exc)) from exc
    return RouteResult(normal_s=normal_s, traffic_s=traffic_s)
