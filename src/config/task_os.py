"""task-os Inbox export knobs (#307). Disabled by default.

A radar-marked WhatsApp message is POSTed to task-os's ``POST /api/tasks`` so it
lands in the Inbox alongside flagged emails (task-os#98). ``token`` is a secret
and lives only in the gitignored ``.env`` (``WR_TASK_OS_TOKEN``), never the
committed defaults.
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


def parse(raw: dict[str, Any]) -> TaskOsConfig:
    return TaskOsConfig(
        enabled=_as_bool(os.environ.get("WR_TASK_OS_ENABLED"), raw.get("enabled", False)),
        base_url=os.environ.get(
            "WR_TASK_OS_BASE_URL", str(raw.get("base_url", "http://127.0.0.1:8448"))
        ),
        token=os.environ.get("WR_TASK_OS_TOKEN", str(raw.get("token", ""))),
        timeout_s=float(raw.get("timeout_s", 6.0)),
    )
