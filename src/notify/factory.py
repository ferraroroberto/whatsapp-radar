"""Construct a notifier from config.

``none`` (the default) yields no notifier, so review records the digest as
``skipped`` exactly as before. ``telegram`` yields a configured
:class:`TelegramNotifier`.
"""

from __future__ import annotations

from src.config import TelegramConfig
from src.notify.base import Notifier
from src.notify.telegram import TelegramNotifier


def build_notifier(name: str, telegram: TelegramConfig) -> Notifier | None:
    """Return a notifier for ``name`` ('none' | 'telegram'), or None for 'none'."""
    if name == "none":
        return None
    if name == "telegram":
        return TelegramNotifier(telegram.bot_token, telegram.chat_id)
    raise ValueError(f"unknown notifier: {name!r} (expected 'none' or 'telegram')")
