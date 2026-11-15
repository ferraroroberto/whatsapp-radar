"""Run links appended to Telegram messages.

The PWA reads ``?run=<id>`` on load and opens that run's sheet, so a message
lands on its decision record instead of the Activity list.
"""

from __future__ import annotations

from src.config import Config

RUN_LINK_PARAM = "run"


def with_run_link(config: Config, text: str, run_id: int | None) -> str:
    """``text`` plus a link to run ``run_id``'s sheet; unchanged without a base URL or id."""
    if run_id is None or not config.public_base_url:
        return text
    return f"{text}\n\n🔎 {config.public_base_url}/?{RUN_LINK_PARAM}={run_id}"
