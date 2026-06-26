"""Notification interface.

Delivery is intentionally decoupled from analysis: a notifier takes an
already-built digest, so a delivery failure never affects message analysis or
cursor state. On failure a notifier raises :class:`NotifierError`; the caller
records the failure and the same run can be re-delivered later (``wr notify``)
without re-analysing anything.

Concrete delivery (Telegram) lives in :mod:`src.notify.telegram`. A
notifier must send only to a non-WhatsApp channel and keep tokens in ignored
config.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.report.digest import Digest


class NotifierError(RuntimeError):
    """Raised when a notifier fails to deliver a digest (so the caller can retry)."""


@runtime_checkable
class Notifier(Protocol):
    def send(self, digest: Digest) -> None:
        """Deliver a consolidated digest. Raises :class:`NotifierError` on failure."""
        ...

    def send_text(self, text: str) -> None:
        """Deliver a one-line operational alert (not a digest).

        Part of the contract so :func:`src.notify.alert.send_alert` reaches every
        notifier without a concrete-type gate — a new notifier that omits it fails
        type-checking rather than silently dropping alerts.
        """
        ...
