"""Presence client contract (#169): fresh / stale-then-refresh / unavailable.

Fully offline — the two HTTP calls are served by a scripted fake session, and no
real coordinates appear anywhere (generic Barcelona-ish decimals only). Covers
the three paths the acceptance criteria name plus person resolution, the
never-trust-``stale`` rule (home-automation#483), and coordinate redaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests

from src.config import PresenceConfig
from src.presence import PresenceLocation, PresenceUnavailable, get_location

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
LAT, LON = 41.55512, 2.34567  # placeholder coords, never a real location


class _Resp:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Session:
    """Scripts a queue of GET snapshots; records POST /refresh calls."""

    def __init__(
        self,
        get_payloads: list[dict[str, Any]],
        *,
        get_exc: Exception | None = None,
        post_exc: Exception | None = None,
    ) -> None:
        self._gets = list(get_payloads)
        self._get_exc = get_exc
        self._post_exc = post_exc
        self.get_calls: list[str] = []
        self.post_calls: list[str] = []

    def get(self, url: str, timeout: float | None = None) -> _Resp:
        self.get_calls.append(url)
        if self._get_exc is not None:
            raise self._get_exc
        return _Resp(self._gets.pop(0))

    def post(self, url: str, timeout: float | None = None) -> _Resp:
        self.post_calls.append(url)
        if self._post_exc is not None:
            raise self._post_exc
        return _Resp({"available": True})


def _entity(
    name: str = "Roberto",
    *,
    age_min: float = 1.0,
    lat: float | None = LAT,
    lon: float | None = LON,
    at_home: bool = False,
    distance_m: float | None = 3200.0,
    role: str | None = None,
    stale: bool = False,
) -> dict[str, Any]:
    last_seen = (NOW - timedelta(minutes=age_min)).isoformat()
    return {
        "entity_id": "e:1", "name": name, "display_name": name, "role": role,
        "latitude": lat, "longitude": lon, "last_seen": last_seen,
        "at_home": at_home, "distance_from_home_m": distance_m, "stale": stale,
    }


def _snapshot(*entities: dict[str, Any]) -> dict[str, Any]:
    return {"available": True, "entities": list(entities)}


def _cfg(**kw: Any) -> PresenceConfig:
    return PresenceConfig(enabled=True, max_age_min=5, **kw)


def test_fresh_fix_returns_location_without_refresh() -> None:
    session = _Session([_snapshot(_entity(age_min=2))])
    result = get_location(_cfg(), "roberto", now=NOW, session=session)
    assert isinstance(result, PresenceLocation)
    assert result.refreshed is False
    assert result.distance_from_home_km == 3.2
    assert result.age_min == pytest.approx(2.0, abs=0.05)
    assert session.post_calls == []  # a fresh fix never forces a locate


def test_stale_fix_refreshes_then_returns_fresh() -> None:
    session = _Session([_snapshot(_entity(age_min=30)), _snapshot(_entity(age_min=1))])
    result = get_location(_cfg(), "roberto", now=NOW, session=session)
    assert isinstance(result, PresenceLocation)
    assert result.refreshed is True
    assert session.post_calls == ["http://127.0.0.1:8447/api/presence/refresh"]
    assert len(session.get_calls) == 2  # re-read after the forced locate


def test_stale_after_refresh_is_unavailable() -> None:
    session = _Session([_snapshot(_entity(age_min=30)), _snapshot(_entity(age_min=25))])
    result = get_location(_cfg(), "roberto", now=NOW, session=session)
    assert isinstance(result, PresenceUnavailable)
    assert result.reason == "stale"
    assert session.post_calls  # a refresh was attempted


def test_disabled_makes_no_http_call() -> None:
    session = _Session([_snapshot(_entity())])
    result = get_location(PresenceConfig(enabled=False), "roberto", now=NOW, session=session)
    assert isinstance(result, PresenceUnavailable)
    assert result.reason == "disabled"
    assert session.get_calls == []


def test_transport_error_degrades_cleanly() -> None:
    session = _Session([], get_exc=requests.ConnectionError("down"))
    result = get_location(_cfg(), "roberto", now=NOW, session=session)
    assert isinstance(result, PresenceUnavailable)
    assert result.reason == "transport_error"


def test_unknown_person_is_not_found_without_refresh() -> None:
    session = _Session([_snapshot(_entity(name="Someone Else"))])
    result = get_location(_cfg(), "roberto", now=NOW, session=session)
    assert isinstance(result, PresenceUnavailable)
    assert result.reason == "not_found"
    assert session.post_calls == []  # not-tracked is final, no locate wasted


def test_no_fix_when_coordinates_missing() -> None:
    no_coords = _snapshot(_entity(lat=None, lon=None))
    session = _Session([no_coords, no_coords])
    result = get_location(_cfg(), "roberto", now=NOW, session=session)
    assert isinstance(result, PresenceUnavailable)
    assert result.reason == "no_fix"
    assert session.post_calls  # missing fix does justify a refresh attempt


def test_api_stale_field_is_never_trusted() -> None:
    # Fresh last_seen but stale=True → treated fresh, no refresh (flag ignored).
    fresh_flagged = _Session([_snapshot(_entity(age_min=2, stale=True))])
    fresh = get_location(_cfg(), "roberto", now=NOW, session=fresh_flagged)
    assert isinstance(fresh, PresenceLocation)
    assert fresh_flagged.post_calls == []
    # Old last_seen but stale=False → refreshed anyway (freshness from last_seen).
    old_unflagged = _Session(
        [_snapshot(_entity(age_min=30, stale=False)), _snapshot(_entity(age_min=1))]
    )
    refreshed = get_location(_cfg(), "roberto", now=NOW, session=old_unflagged)
    assert isinstance(refreshed, PresenceLocation) and refreshed.refreshed is True


def test_resolution_via_role_alias() -> None:
    session = _Session([_snapshot(_entity(name="Device 1", role="dad", age_min=1))])
    cfg = _cfg(person_aliases={"roberto": ("dad",)})
    result = get_location(cfg, "roberto", now=NOW, session=session)
    assert isinstance(result, PresenceLocation)


def test_repr_redacts_coordinates() -> None:
    session = _Session([_snapshot(_entity(age_min=1))])
    result = get_location(_cfg(), "roberto", now=NOW, session=session)
    text = repr(result)
    assert "redacted" in text
    assert str(LAT) not in text and str(LON) not in text
