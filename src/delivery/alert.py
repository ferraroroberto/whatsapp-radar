"""Out-of-band operational alerts (not digests).

A digest tells the operator *what's actionable*; an alert tells them *the system
couldn't do its job*. The motivating case: a scheduled live scan
aborts because the WhatsApp sidecar is offline — without an alert that failure is
invisible (a green-looking job that checked nothing). This sends a one-line text
through the same configured channel as the digest, best-effort: it never raises,
so an alert failure can't worsen the situation it's reporting.
"""

from __future__ import annotations

from src.config import Config
from src.delivery.dispatch import dispatch
from src.delivery.links import with_run_link


def send_alert(
    config: Config, text: str, *, run_id: int | None = None
) -> tuple[str, str | None]:
    """Send an operational alert via the configured notifier. Returns ``(status, detail)``.

    ``status`` is ``'sent'`` | ``'skipped'`` (no notifier configured) |
    ``'failed'``. Never raises — callers fire this on a path that is already
    failing. ``run_id`` appends a link to that run's sheet when a public base URL
    is configured.
    """
    body = with_run_link(config, text, run_id)
    return dispatch(config, lambda notifier: notifier.send_text(body))
