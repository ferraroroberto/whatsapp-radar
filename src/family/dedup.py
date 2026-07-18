"""Persistent dedup log for traffic alerts (issue #160).

The #1 production failure of the OpenClaw version was a silently-failing dedup
(6 duplicate alerts in 8 minutes for one route). This is the deterministic
replacement: an append-only JSONL under the ignored ``data/`` path recording
``{key, ts}`` for every alert actually sent, and a bounded read of keys still
inside the dedup window. One canonical key schema (:func:`rules.dedup_key`)
removes the failure mode rather than needing to be "more careful".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


def default_path() -> Path:
    from src.config import project_root

    return project_root() / "data" / "family" / "traffic_alerts.jsonl"


def recent_keys(within_min: int, *, now: datetime, path: Path | None = None) -> set[str]:
    """Alert keys recorded within the last ``within_min`` minutes."""
    target = path or default_path()
    if not target.is_file():
        return set()
    cutoff = now - timedelta(minutes=within_min)
    keys: set[str] = set()
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        raw_ts, key = record.get("ts"), record.get("key")
        if not raw_ts or not key:
            continue
        try:
            when = datetime.fromisoformat(str(raw_ts))
        except ValueError:
            continue
        if when >= cutoff:
            keys.add(str(key))
    return keys


def record_alert(key: str, *, now: datetime, path: Path | None = None) -> None:
    """Append one sent-alert record."""
    target = path or default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": key, "ts": now.isoformat()}, ensure_ascii=False) + "\n")
