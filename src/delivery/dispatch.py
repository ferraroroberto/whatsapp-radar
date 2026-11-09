"""Build the configured notifier and run one send against it."""

from __future__ import annotations

from collections.abc import Callable

from src.config import Config
from src.notify import Notifier, NotifierError, build_notifier


def dispatch(config: Config, send: Callable[[Notifier], None]) -> tuple[str, str | None]:
    """Build the configured notifier and hand it to ``send``. Returns ``(status, detail)``.

    ``status`` is ``'sent'`` | ``'skipped'`` (no notifier configured) |
    ``'failed'``. Never raises: shared by :func:`src.delivery.alert.send_alert`
    (best-effort, fires on paths already failing) and
    :func:`src.delivery.digest.deliver_digest` (which adds its own
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
