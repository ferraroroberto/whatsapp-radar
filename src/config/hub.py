"""Local-llm-hub classification client config."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HubConfig:
    base_url: str
    model: str
    # Output token budget for one classification call. Sized per model rather
    # than hard-coded so a reasoning model with a long <think> trace can be given
    # room instead of silently truncating mid-think.
    max_tokens: int = 8192
    # Max characters of the rendered message delta sent in one prompt. Caps a
    # whole-history scan so a single request can't blow the model's context.
    max_prompt_chars: int = 24000
    # How many days of already-surfaced actionable alerts to feed Stage 2 as
    # short-term memory, so a repeated to-do isn't re-alerted every run (#66).
    recent_alert_days: int = 7


def parse(raw: dict[str, Any]) -> HubConfig:
    return HubConfig(
        base_url=os.environ.get("WR_HUB_BASE_URL", raw.get("base_url", "http://127.0.0.1:8000")),
        model=os.environ.get("WR_HUB_MODEL", raw.get("model", "claude_sonnet")),
        max_tokens=int(os.environ.get("WR_HUB_MAX_TOKENS", raw.get("max_tokens", 8192))),
        max_prompt_chars=int(
            os.environ.get("WR_HUB_MAX_PROMPT_CHARS", raw.get("max_prompt_chars", 24000))
        ),
        recent_alert_days=int(
            os.environ.get("WR_HUB_RECENT_ALERT_DAYS", raw.get("recent_alert_days", 7))
        ),
    )
