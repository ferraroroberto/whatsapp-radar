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


def send_alert(config: Config, text: str) -> tuple[str, str | None]:
    """Send an operational alert via the configured notifier. Returns ``(status, detail)``.

    ``status`` is ``'sent'`` | ``'skipped'`` (no notifier configured) |
    ``'failed'``. Never raises — callers fire this on a path that is already
    failing. Every notifier implements ``send_text`` (part of the ``Notifier``
    contract), so an alert is never silently dropped for a wired notifier.
    """
    try:
        notifier = build_notifier(config.notifier, config.telegram)
    except (NotifierError, ValueError) as exc:
        return "failed", str(exc)
    if notifier is None:
        return "skipped", "no notifier (none)"
    try:
        notifier.send_text(text)
    except NotifierError as exc:
        return "failed", str(exc)
    return "sent", None
