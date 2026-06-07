"""Out-of-band operational alerts (not digests).

A digest tells the operator *what's actionable*; an alert tells them *the system
couldn't do its job*. The motivating case (issue #29): a scheduled live scan
aborts because the WhatsApp sidecar is offline — without an alert that failure is
invisible (a green-looking job that checked nothing). This sends a one-line text
through the same configured channel as the digest, best-effort: it never raises,
so an alert failure can't worsen the situation it's reporting.
"""

from __future__ import annotations

from src.config import Config
from src.notify.base import NotifierError
from src.notify.factory import build_notifier
from src.notify.telegram import TelegramNotifier


def send_alert(config: Config, text: str) -> tuple[str, str | None]:
    """Send an operational alert via the configured notifier. Returns ``(status, detail)``.

    ``status`` is ``'sent'`` | ``'skipped'`` (no notifier, or it has no text
    channel) | ``'failed'``. Never raises — callers fire this on a path that is
    already failing.
    """
    try:
        notifier = build_notifier(config.notifier, config.telegram)
    except (NotifierError, ValueError) as exc:
        return "failed", str(exc)
    if notifier is None:
        return "skipped", "no notifier (none)"
    if isinstance(notifier, TelegramNotifier):
        try:
            notifier.send_text(text)
        except NotifierError as exc:
            return "failed", str(exc)
        return "sent", None
    return "skipped", "notifier has no text channel"
