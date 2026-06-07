"""Structured result contract between a launched CLI run and its watcher.

Every Execution-tab action runs as a subprocess (``python launcher.py <cmd>``)
whose combined stdout+stderr is streamed to ``output.log`` for live viewing. The
human-readable progress lines are for the operator; the *structured* result (the
scan funnel, the resync delta, the reprocess summary) is emitted as one final
sentinel line the webapp parses back into JSON.

A sentinel line keeps one stream serving both purposes — byte-by-byte live output
*and* a machine-readable outcome — without a second IPC channel. The CLI calls
:func:`format_result`; the webapp run-record reader calls :func:`parse_result`.
"""

from __future__ import annotations

import json
from typing import Any

# Prefix chosen to be unmistakable in a log and trivially greppable. The whole
# result is one line so a tail that truncates earlier output still finds it.
RESULT_SENTINEL = "__WR_RESULT__"


def format_result(payload: dict[str, Any]) -> str:
    """Render a result payload as the single sentinel line to print last."""
    return f"{RESULT_SENTINEL} {json.dumps(payload, ensure_ascii=False)}"


def parse_result(text: str) -> dict[str, Any] | None:
    """Extract the structured result from captured output, or None if absent.

    Scans from the end so the *last* sentinel wins (a run emits exactly one, but
    being last-write-wins is robust to anything odd upstream).
    """
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith(RESULT_SENTINEL):
            body = stripped[len(RESULT_SENTINEL):].strip()
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None
