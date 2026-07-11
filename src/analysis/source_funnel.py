"""Shared persisted per-source run-funnel contract."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass
class SourceFunnel:
    """One source's truthful path through a scan or process run."""

    sync_status: str = "skipped"
    sync_error: str | None = None
    chats_synced: int = 0
    messages_synced: int = 0
    monitored_channels: int = 0
    channels_with_delta: int = 0
    messages_checked: int = 0
    stage1_passed: int = 0
    stage1_rejected: int = 0
    llm_calls: int = 0
    actionable: int = 0
    cursors_advanced: int = 0


def ensure_source_funnel(
    funnels: dict[str, SourceFunnel], source: str
) -> SourceFunnel:
    """Return one source funnel, creating its skipped/zero state when absent."""
    return funnels.setdefault(source, SourceFunnel())


def source_funnels_dict(
    funnels: dict[str, SourceFunnel],
) -> dict[str, dict[str, object]]:
    """Convert source funnels to the stable API/storage mapping."""
    return {source: asdict(funnel) for source, funnel in funnels.items()}


def source_funnels_json(funnels: dict[str, SourceFunnel]) -> str:
    """Serialize source funnels for ``review_runs.source_funnel_json``."""
    return json.dumps(source_funnels_dict(funnels), ensure_ascii=False)
