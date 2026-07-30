"""Telegram notifier credentials.

Secrets live in the gitignored ``config/webapp_config.json`` so the webapp UI
owns them. Precedence: ``WR_TELEGRAM_*`` env > webapp_config > local.json/default.json.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str


def parse(raw: dict[str, Any]) -> TelegramConfig:
    # Imported lazily to avoid a config import cycle.
    from src.webapp_config import load_webapp_config

    wcfg = load_webapp_config()
    bot_default = wcfg.telegram_bot_token or raw.get("bot_token", "")
    chat_default = wcfg.telegram_chat_id or raw.get("chat_id", "")
    return TelegramConfig(
        bot_token=os.environ.get("WR_TELEGRAM_BOT_TOKEN", bot_default),
        chat_id=os.environ.get("WR_TELEGRAM_CHAT_ID", chat_default),
    )
