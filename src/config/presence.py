"""Live phone-location lookup via home-automation's presence API (#169)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from src.config._shared import _as_bool


@dataclass(frozen=True)
class PresenceConfig:
    """Live phone-location lookup via home-automation's presence API (#169).

    Read-only cross-repo dependency: ``GET {base_url}/api/presence`` returns each
    tracked device's ``last_seen``/``latitude``/``longitude``/``at_home``, and
    ``POST {base_url}/api/presence/refresh`` forces a fresh Find My locate.
    Loopback callers bypass its bearer token. Disabled by default so the suite
    stays fully offline and the family checks keep working with no home-automation
    running (the calendar-inference chain is the documented fallback).

    Freshness is always derived client-side from ``last_seen`` — the API's own
    ``stale`` flag is hard-coded ``false`` for iCloud entities (home-automation#483)
    and must never be trusted. ``person_aliases`` maps a whatsapp-radar person key
    (e.g. ``"roberto"``) to extra names/roles the presence API might carry for the
    same person (e.g. ``["dad"]``); the person key itself already matches the
    entity's display name, so aliases are only needed for role-based resolution.
    """

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8447"
    # TLS certificate verification for an https base_url. Keep True except for
    # the loopback deployment: home-automation serves :8447 with its Tailscale
    # certificate, whose ts.net hostname can never match ``127.0.0.1`` — and the
    # hostname-verified path (https://<host>.ts.net:8447) forfeits the loopback
    # auth bypass (401 without a bearer token). False is safe only because the
    # loopback hop never leaves the machine (#177).
    verify_tls: bool = True
    # A fix older than this many minutes is stale and triggers a forced refresh.
    max_age_min: int = 5
    # Per-request read timeout for the cached-snapshot GET.
    timeout_s: float = 6.0
    # The forced-locate POST does a real Apple round-trip, so it gets a longer bound.
    refresh_timeout_s: float = 12.0
    person_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)


def parse(raw: dict[str, Any]) -> PresenceConfig:
    aliases: dict[str, tuple[str, ...]] = {}
    for person, names in (raw.get("person_aliases") or {}).items():
        key = str(person).strip().lower()
        if not key:
            continue
        values = names if isinstance(names, (list, tuple)) else [names]
        cleaned = tuple(str(v).strip() for v in values if str(v).strip())
        if cleaned:
            aliases[key] = cleaned
    return PresenceConfig(
        enabled=_as_bool(os.environ.get("WR_PRESENCE_ENABLED"), raw.get("enabled", False)),
        base_url=os.environ.get(
            "WR_PRESENCE_BASE_URL", str(raw.get("base_url", "http://127.0.0.1:8447"))
        ),
        verify_tls=_as_bool(os.environ.get("WR_PRESENCE_VERIFY_TLS"), raw.get("verify_tls", True)),
        max_age_min=int(raw.get("max_age_min", 5)),
        timeout_s=float(raw.get("timeout_s", 6.0)),
        refresh_timeout_s=float(raw.get("refresh_timeout_s", 12.0)),
        person_aliases=aliases,
    )
