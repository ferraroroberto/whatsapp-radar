"""Routes v2 request-shape contract for `compute_route` (issues #160, #266).

Offline: the HTTP session is a stub that records the request body — nothing
leaves the process, and no API key is needed.

The travel-block sweep (#266) prices a leg as a **departure**, so the body it
sends has to carry ``departureTime`` and not ``arrivalTime``. That distinction
is not cosmetic: probing the live API showed Routes silently ignores
``arrivalTime`` for ``DRIVE`` + ``TRAFFIC_AWARE`` (every arrival variant of one
fixed route returned the depart-now baseline unchanged, while departures at
04:00 and 08:00 differed by ~9%). A regression that swapped the field back
would therefore not fail loudly — it would quietly price every future drive as
if it started now. Hence a test on the wire format itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.traffic import TrafficReadError, compute_route

ORIGIN = "1 Example Street, Sample Town"
DESTINATION = "3 Example Road, Sample City"
WHEN = datetime(2026, 7, 20, 8, 30, tzinfo=UTC)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    """Records the one POST `compute_route` makes and replays a canned answer."""

    def __init__(self, payload: dict[str, Any] | None = None, status_code: int = 200) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._payload = payload if payload is not None else {
            "routes": [{"duration": "1200s", "staticDuration": "1000s"}]
        }
        self._status_code = status_code

    def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: int
    ) -> _FakeResponse:
        self.bodies.append(json)
        return _FakeResponse(self._payload, self._status_code)

    @property
    def body(self) -> dict[str, Any]:
        assert len(self.bodies) == 1, f"expected exactly one Routes call, got {len(self.bodies)}"
        return self.bodies[0]


def _call(**kwargs: Any) -> _FakeSession:
    session = _FakeSession()
    compute_route(ORIGIN, DESTINATION, api_key="k", session=session, **kwargs)  # type: ignore[arg-type]
    return session


def test_departure_time_is_sent_as_departure_time_and_never_as_arrival_time() -> None:
    body = _call(departure_time=WHEN).body
    assert body["departureTime"] == WHEN.astimezone().isoformat()
    assert "arrivalTime" not in body
    assert body["routingPreference"] == "TRAFFIC_AWARE"
    assert body["travelMode"] == "DRIVE"


def test_arrival_time_still_sends_arrival_time_for_the_existing_traffic_check() -> None:
    """#266 must not change what the #160 alerting path puts on the wire."""
    body = _call(arrival_time=WHEN).body
    assert body["arrivalTime"] == WHEN.astimezone().isoformat()
    assert "departureTime" not in body


def test_neither_time_field_means_depart_now() -> None:
    body = _call().body
    assert "arrivalTime" not in body and "departureTime" not in body


def test_arrival_and_departure_together_are_refused() -> None:
    """Routes returns 200 and silently honours the departure — so refuse locally."""
    session = _FakeSession()
    with pytest.raises(TrafficReadError, match="mutually exclusive"):
        compute_route(
            ORIGIN, DESTINATION, api_key="k",
            arrival_time=WHEN, departure_time=WHEN,
            session=session,  # type: ignore[arg-type]
        )
    assert session.bodies == [], "the request must not be sent at all"


def test_empty_route_list_is_an_error_not_a_zero_duration() -> None:
    """The #266 failure path depends on this raising rather than returning 0."""
    session = _FakeSession(payload={"routes": []})
    with pytest.raises(TrafficReadError, match="no route"):
        compute_route(
            ORIGIN, DESTINATION, api_key="k", departure_time=WHEN,
            session=session,  # type: ignore[arg-type]
        )


def test_non_200_is_a_privacy_safe_error() -> None:
    session = _FakeSession(payload={"error": {"message": "quota"}}, status_code=429)
    with pytest.raises(TrafficReadError) as excinfo:
        compute_route(
            ORIGIN, DESTINATION, api_key="k", departure_time=WHEN,
            session=session,  # type: ignore[arg-type]
        )
    detail = str(excinfo.value)
    assert "429" in detail
    assert ORIGIN not in detail and DESTINATION not in detail and "k" != detail
