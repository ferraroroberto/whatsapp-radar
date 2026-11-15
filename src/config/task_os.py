"""task-os Inbox export knobs. Disabled by default.

A radar-marked WhatsApp message is POSTed to task-os's ``POST /api/tasks`` so it
lands in the Inbox alongside flagged emails. ``token`` is a secret
and lives only in the gitignored ``.env`` (``WR_TASK_OS_TOKEN``), never the
committed defaults.

``auto_export`` is the opt-in that has a scan send each actionable verdict's
evidence messages on its own, instead of waiting for a tap per message. It only
takes effect while export itself is enabled and the token is set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.config._shared import _as_bool


@dataclass(frozen=True)
class TaskOsConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8448"
    token: str = ""
    timeout_s: float = 6.0
    auto_export: bool = False

    @property
    def ready(self) -> bool:
        """Export is switched on and has the bearer token it needs."""
        return self.enabled and bool(self.token)


def parse(raw: dict[str, Any]) -> TaskOsConfig:
    defaults = TaskOsConfig()
    return TaskOsConfig(
        enabled=_as_bool(
            os.environ.get("WR_TASK_OS_ENABLED"), raw.get("enabled", defaults.enabled)
        ),
        base_url=os.environ.get(
            "WR_TASK_OS_BASE_URL", str(raw.get("base_url", defaults.base_url))
        ),
        token=os.environ.get("WR_TASK_OS_TOKEN", str(raw.get("token", defaults.token))),
        timeout_s=float(raw.get("timeout_s", defaults.timeout_s)),
        auto_export=_as_bool(
            os.environ.get("WR_TASK_OS_AUTO_EXPORT"),
            raw.get("auto_export", defaults.auto_export),
        ),
    )
