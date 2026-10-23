"""Bounds and optional notification cadence for unmonitored-chat signals."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.config._shared import _as_bool


@dataclass(frozen=True)
class TripwireConfig:
    """Bounds and optional notification cadence for unmonitored-chat signals."""

    window_days: int = 7
    max_messages: int = 500
    max_messages_per_chat: int = 20
    # In-app suggestions are always available. Telegram stays silent unless this
    # independent opt-in is set; normal notifier credentials/config still apply.
    telegram_nudge_enabled: bool = False
    nudge_cadence_days: int = 7


def parse(raw: dict[str, Any]) -> TripwireConfig:
    defaults = TripwireConfig()
    return TripwireConfig(
        window_days=max(
            1,
            int(
                os.environ.get(
                    "WR_TRIPWIRE_WINDOW_DAYS", raw.get("window_days", defaults.window_days)
                )
            ),
        ),
        max_messages=max(
            1,
            int(
                os.environ.get(
                    "WR_TRIPWIRE_MAX_MESSAGES", raw.get("max_messages", defaults.max_messages)
                )
            ),
        ),
        max_messages_per_chat=max(
            1,
            int(
                os.environ.get(
                    "WR_TRIPWIRE_MAX_MESSAGES_PER_CHAT",
                    raw.get("max_messages_per_chat", defaults.max_messages_per_chat),
                )
            ),
        ),
        telegram_nudge_enabled=_as_bool(
            os.environ.get("WR_TRIPWIRE_TELEGRAM_NUDGE_ENABLED"),
            raw.get("telegram_nudge_enabled", defaults.telegram_nudge_enabled),
        ),
        nudge_cadence_days=max(
            1,
            int(
                os.environ.get(
                    "WR_TRIPWIRE_NUDGE_CADENCE_DAYS",
                    raw.get("nudge_cadence_days", defaults.nudge_cadence_days),
                )
            ),
        ),
    )
