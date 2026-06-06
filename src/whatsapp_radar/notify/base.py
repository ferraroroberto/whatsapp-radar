"""Notification interface.

Delivery is intentionally decoupled from analysis: a notifier takes an
already-built digest and is responsible for its own retries, so a delivery
failure never affects message analysis or cursor state.

The concrete Telegram delivery is DEFERRED to a follow-up issue (onboarding step
8). It must send only to a non-WhatsApp channel, keep tokens in ignored config,
and be retryable independently of the review run.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..report.digest import Digest


@runtime_checkable
class Notifier(Protocol):
    def send(self, digest: Digest) -> None:
        """Deliver a consolidated digest. Implementations own their retry policy."""
        ...
