"""Traffic-jam check knobs (Google Routes API v2). Disabled by default (#160)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.config._shared import _as_bool


@dataclass(frozen=True)
class TrafficConfig:
    """Traffic-jam check knobs (Google Routes API v2). Disabled by default (#160).

    ``api_key`` is a secret and lives only in the ignored ``config/local.json``
    (or ``WR_TRAFFIC_API_KEY`` / ``GOOGLE_MAPS_API_KEY``), never the committed
    defaults. Quiet hours pause checks overnight; only a delay over
    ``significant_delay_min`` alerts, deduped within ``dedup_window_min``.
    """

    enabled: bool = False
    api_key: str = ""
    significant_delay_min: int = 15
    quiet_start_hour: int = 20  # local hour checks pause at (inclusive)
    quiet_end_hour: int = 5  # local hour checks resume at
    # Raised from 30 in #252. At 30 it exactly equalled `cadence_min`, so a
    # record written one cadence ago sat precisely on the window boundary and
    # any scheduler jitter pushed it out — the same event re-alerted on nearly
    # every check (four "Tight schedule" messages for one event in two hours).
    # See `effective_dedup_window_min` for the floor that makes that
    # unexpressible regardless of what is configured here.
    dedup_window_min: int = 180
    origin_lookback_min: int = 60
    lookahead_hours: int = 3  # how far ahead to look for the next commute
    # Slack (minutes) baked into the "leave now" alert (#185): the alert fires
    # when `event.start - (now + eta + leave_margin_min) <= 0`, i.e. a few
    # minutes *before* the last possible departure so the person has a moment to
    # move. Only a live phone fix can trigger it — a calendar-inference origin
    # makes no claim about where the person actually is. Its timeliness is
    # bounded by `cadence_min`: the alert lands on the first check after the
    # departure moment, so set a low cadence when relying on leave-now.
    leave_margin_min: int = 5
    # Train-commute suppression (#227, widened in #252). Both the leave-now and
    # the infeasible-hop ("Tight schedule") alerts are *driving* judgments —
    # their minutes come from a Routes DRIVE request — so both are noise for a
    # commute taken by train (the daily office run, titled e.g. "trabajo en la
    # oficina (en tren)"): telling someone the drive is ~10 min says nothing
    # about their train. When on, an event whose title contains one of
    # `train_keywords` fires neither. The *delay* alert is deliberately still
    # untouched: a significant delay on the road is at least a real-world signal
    # even if this person is not driving. Keywords are configurable so a genuine
    # *drive to the train station* can be tuned out without a code change.
    #
    # The key keeps its original name: renaming it would churn the API payload,
    # the Family tab's toggle, and every persisted config for a purely cosmetic
    # gain. The UI label ("Skip train commutes") already describes the widened
    # behaviour accurately.
    skip_leave_now_for_train: bool = True
    train_keywords: tuple[str, ...] = ("tren", "train")
    # How often a live check should actually run (#164). The webapp persists
    # this; the App Launcher job (`family-radar-traffic-check`, #170) is armed
    # at a fixed high frequency (every few minutes) regardless, and `wr
    # traffic-check` self-skips in-process when fewer than `cadence_min`
    # minutes have elapsed since the last recorded traffic-check run — so
    # editing this value here takes effect immediately, with no Task
    # Scheduler re-arm needed.
    cadence_min: int = 30

    @property
    def effective_dedup_window_min(self) -> int:
        """The dedup window actually applied — never shorter than the lookahead.

        A single event stays inside the lookahead window for `lookahead_hours`
        and is re-checked every `cadence_min` throughout, so a dedup window
        shorter than the lookahead *mathematically guarantees* the same event
        alerts more than once (#252). The floor is derived from that, not a
        magic multiplier: one approach to one event earns one alert.

        Configuring a shorter window is therefore treated as a misconfiguration
        and quietly raised rather than silently doing nothing — the caller logs
        whenever it is raised, so the override is visible.
        """
        return max(self.dedup_window_min, self.lookahead_hours * 60)


def parse(raw: dict[str, Any]) -> TrafficConfig:
    api_key = (
        os.environ.get("WR_TRAFFIC_API_KEY")
        or os.environ.get("GOOGLE_MAPS_API_KEY")
        or str(raw.get("api_key", ""))
    )
    raw_keywords = raw.get("train_keywords")
    if isinstance(raw_keywords, (list, tuple)):
        keywords = tuple(str(k).strip().lower() for k in raw_keywords if str(k).strip())
    else:
        keywords = TrafficConfig.train_keywords
    return TrafficConfig(
        enabled=_as_bool(os.environ.get("WR_TRAFFIC_ENABLED"), raw.get("enabled", False)),
        api_key=api_key,
        skip_leave_now_for_train=bool(raw.get("skip_leave_now_for_train", True)),
        train_keywords=keywords,
        significant_delay_min=int(raw.get("significant_delay_min", 15)),
        quiet_start_hour=int(raw.get("quiet_start_hour", 20)),
        quiet_end_hour=int(raw.get("quiet_end_hour", 5)),
        dedup_window_min=int(raw.get("dedup_window_min", 180)),
        origin_lookback_min=int(raw.get("origin_lookback_min", 60)),
        lookahead_hours=int(raw.get("lookahead_hours", 3)),
        cadence_min=int(raw.get("cadence_min", 30)),
        leave_margin_min=int(raw.get("leave_margin_min", 5)),
    )
