"""Presence client — the responsible parent's live phone location (issue #169).

One public seam, :func:`get_location`, over home-automation's read-only presence
API (``GET /api/presence`` cached snapshot + ``POST /api/presence/refresh`` forced
locate; loopback bypasses its bearer token). It resolves a whatsapp-radar person
key to a tracked entity, derives the fix's freshness *from ``last_seen``* — never
from the API's own ``stale`` field, which is hard-coded ``false`` for iCloud
entities (home-automation#483) — and:

- returns a :class:`PresenceLocation` when the fix is fresh (≤ ``max_age_min``);
- forces one bounded refresh and re-reads when it is stale, returning the fresh
  fix if the locate delivered one;
- returns a typed :class:`PresenceUnavailable` for every other outcome (feature
  off, transport error, person not tracked, no usable fix, still stale after
  refresh) so the caller falls back to calendar inference instead of erroring.

Deterministic and side-effect-free apart from the two HTTP calls; ``now`` and the
HTTP ``session`` are injected so the whole thing tests offline with a mock.
"""

from __future__ import annotations

import logging
import unicodedata
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests
from urllib3.exceptions import InsecureRequestWarning

from src.config import PresenceConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PresenceLocation:
    """A fresh, usable phone fix — coordinates for transient routing use only.

    ``latitude``/``longitude`` exist so one Routes request can be built from the
    real position; they are never persisted. ``distance_from_home_km`` and
    ``age_min`` are the derived, privacy-safe values a run trace may record.
    """

    person: str
    latitude: float
    longitude: float
    at_home: bool
    distance_from_home_km: float | None
    age_min: float
    refreshed: bool

    @property
    def available(self) -> bool:
        return True

    def __repr__(self) -> str:  # never leak raw coordinates into a log line
        return (
            f"PresenceLocation(person={self.person!r}, at_home={self.at_home}, "
            f"distance_km={self.distance_from_home_km}, age_min={self.age_min:.1f}, "
            f"refreshed={self.refreshed}, coords=<redacted>)"
        )


@dataclass(frozen=True)
class PresenceUnavailable:
    """No usable live location, with a privacy-safe reason for the trace/log.

    ``reason`` ∈ ``disabled`` | ``transport_error`` | ``not_found`` | ``no_fix``
    | ``stale`` — the last two only after a forced refresh failed to deliver a
    fresh fix. ``detail`` carries no coordinates, only diagnostic context.
    """

    person: str
    reason: str
    detail: str = ""

    @property
    def available(self) -> bool:
        return False


PresenceResult = PresenceLocation | PresenceUnavailable


def _fold(text: str) -> str:
    """Comparison key: lowercased, accents stripped (matches "Ana" ⇢ "ana")."""
    lowered = unicodedata.normalize("NFD", (text or "").strip().lower())
    return "".join(ch for ch in lowered if not unicodedata.combining(ch))


def _match_entities(
    entities: list[dict[str, Any]], person: str, aliases: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Every entity for ``person`` by role / display name / raw name.

    The whatsapp-radar person key (e.g. ``"roberto"``) already folds to the
    entity's ``display_name`` (``"Roberto"``); ``aliases`` add role-based hits
    (``"dad"``) or any other spelling the presence source uses. All matches are
    returned because one person appears as several entities in the live payload
    (per-device rows across two accounts plus derived role/"shortcut" people
    entries that carry ``at_home`` but no coordinates) — the caller picks the
    best usable fix rather than trusting whichever happens to be listed first
    (#177: first-match shadowed the real device fix behind a coordinate-less
    role entry on the very first live run).
    """
    wanted = {_fold(person), *(_fold(a) for a in aliases)}
    wanted.discard("")
    if not wanted:
        return []
    matched: list[dict[str, Any]] = []
    for entity in entities:
        candidates = {
            _fold(str(entity.get(field) or ""))
            for field in ("role", "display_name", "name", "entity_id")
        }
        candidates.discard("")
        if wanted & candidates:
            matched.append(entity)
    return matched


@contextmanager
def _tls_warning_guard(verify: bool) -> Iterator[None]:
    """Silence urllib3's per-request insecure warning only when ``verify_tls``
    was deliberately turned off (the loopback Tailscale-cert case, #177) —
    a verified deployment keeps every warning."""
    if verify:
        yield
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", InsecureRequestWarning)
        yield


def _get(
    url: str, timeout: float, session: requests.Session | None, *, verify: bool
) -> dict[str, Any]:
    http = session or requests
    with _tls_warning_guard(verify):
        response = http.get(url, timeout=timeout, verify=verify)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


def _post(url: str, timeout: float, session: requests.Session | None, *, verify: bool) -> None:
    http = session or requests
    with _tls_warning_guard(verify):
        response = http.post(url, timeout=timeout, verify=verify)
    response.raise_for_status()


def _evaluate(
    config: PresenceConfig,
    person: str,
    data: dict[str, Any],
    now: datetime,
    *,
    refreshed: bool,
) -> PresenceResult:
    """Resolve + freshness-check one snapshot. Pure over the fetched payload."""
    entities = data.get("entities") or []
    aliases = config.person_aliases.get(person) or config.person_aliases.get(_fold(person)) or ()
    matched = _match_entities(entities, person, aliases)
    if not matched:
        return PresenceUnavailable(person, "not_found")

    # Among all of the person's entities, use the freshest one that actually
    # carries a fix — coordinate-less rows (role/"shortcut" entries, offline
    # accessories) must never shadow a live device (#177).
    usable: list[tuple[datetime, dict[str, Any]]] = []
    for entity in matched:
        if entity.get("latitude") is None or entity.get("longitude") is None:
            continue
        raw = entity.get("last_seen")
        if not raw:
            continue
        try:
            seen = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        usable.append((seen, entity))
    if not usable:
        return PresenceUnavailable(person, "no_fix")
    last_seen, entity = max(usable, key=lambda pair: pair[0])
    lat, lon = entity.get("latitude"), entity.get("longitude")

    # Freshness is derived here, never read from entity["stale"] (home-automation#483).
    age_min = (now - last_seen).total_seconds() / 60.0
    if age_min > config.max_age_min:
        return PresenceUnavailable(person, "stale", f"age {age_min:.1f}m")

    distance_m = entity.get("distance_from_home_m")
    distance_km = (
        round(float(distance_m) / 1000.0, 1)
        if isinstance(distance_m, (int, float))
        else None
    )
    return PresenceLocation(
        person=person,
        latitude=float(lat),
        longitude=float(lon),
        at_home=bool(entity.get("at_home")),
        distance_from_home_km=distance_km,
        age_min=round(max(0.0, age_min), 1),
        refreshed=refreshed,
    )


def get_location(
    config: PresenceConfig,
    person: str,
    *,
    now: datetime,
    session: requests.Session | None = None,
) -> PresenceResult:
    """Return the person's live phone location, or a typed reason it's unavailable.

    Three outcomes the caller must handle: a fresh :class:`PresenceLocation`
    (possibly after a forced refresh), or a :class:`PresenceUnavailable` telling
    it to fall back. Never raises for an expected failure — a down or slow
    home-automation degrades to ``transport_error``.
    """
    if not config.enabled:
        return PresenceUnavailable(person, "disabled")

    base = config.base_url.rstrip("/")
    verify = config.verify_tls
    try:
        data = _get(f"{base}/api/presence", config.timeout_s, session, verify=verify)
    except (requests.RequestException, ValueError) as exc:
        logger.info("ℹ️ presence read failed for %s: %s", person, type(exc).__name__)
        return PresenceUnavailable(person, "transport_error", type(exc).__name__)

    result = _evaluate(config, person, data, now, refreshed=False)
    # A fresh fix, or a person we don't track, is final — only stale / no-fix
    # justifies the extra Apple round-trip of a forced refresh.
    if isinstance(result, PresenceLocation) or result.reason == "not_found":
        return result

    try:
        _post(f"{base}/api/presence/refresh", config.refresh_timeout_s, session, verify=verify)
        data = _get(f"{base}/api/presence", config.timeout_s, session, verify=verify)
    except (requests.RequestException, ValueError) as exc:
        logger.info("ℹ️ presence refresh failed for %s: %s", person, type(exc).__name__)
        return PresenceUnavailable(person, "transport_error", type(exc).__name__)

    return _evaluate(config, person, data, now, refreshed=True)
