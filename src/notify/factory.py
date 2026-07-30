"""Construct a notifier from config.

``none`` (the default) yields no notifier, so review records the digest as
``skipped`` exactly as before. ``telegram`` yields a configured
:class:`TelegramNotifier`.
"""

from __future__ import annotations

from collections.abc import Callable

from src.config import Config, TelegramConfig
from src.notify.base import Notifier, NotifierError
from src.notify.telegram import TelegramNotifier


def build_notifier(name: str, telegram: TelegramConfig) -> Notifier | None:
    """Return a notifier for ``name`` ('none' | 'telegram'), or None for 'none'."""
    if name == "none":
        return None
    if name == "telegram":
        return TelegramNotifier(telegram.bot_token, telegram.chat_id)
    raise ValueError(f"unknown notifier: {name!r} (expected 'none' or 'telegram')")


def _dispatch(config: Config, send: Callable[[Notifier], None]) -> tuple[str, str | None]:
    """Build the configured notifier and hand it to ``send``. Returns ``(status, detail)``.

    ``status`` is ``'sent'`` | ``'skipped'`` (no notifier configured) |
    ``'failed'``. Never raises: shared by :func:`src.notify.alert.send_alert`
    (best-effort, fires on paths already failing) and
    :func:`src.notify.delivery.deliver_digest` (which adds its own
    ``record_notification`` persistence around the result).
    """
    try:
        notifier = build_notifier(config.notifier, config.telegram)
    except (NotifierError, ValueError) as exc:
        return "failed", str(exc)
    if notifier is None:
        return "skipped", "no notifier (none)"
    try:
        send(notifier)
    except NotifierError as exc:
        return "failed", str(exc)
    return "sent", None
