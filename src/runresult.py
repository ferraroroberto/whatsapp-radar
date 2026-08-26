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

#: Shown in place of the sentinel line when output is filtered for display, so
#: an operator sees that something was withheld rather than one fewer line.
RESULT_WITHHELD_NOTE = (
    "(structured result payload withheld from this view — see the funnel "
    "above, or the Audit tab's redacted dump for a family-check run)"
)


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


def strip_result_line(text: str) -> str:
    """Remove the sentinel result line from output meant for a human to read.

    The sentinel line is how the webapp *parses* a run's structured result
    (:func:`parse_result`) — but it is that whole result payload serialised
    verbatim, including fields (a calendar's id, a travel leg's street
    address) no operator-facing surface is allowed to paint into the DOM
    (#292). A caller that needs the structured result reads the untouched log
    via :func:`parse_result`; this is only for text a human will see.
    """
    lines = text.splitlines()
    kept = [line for line in lines if not line.strip().startswith(RESULT_SENTINEL)]
    if len(kept) != len(lines):
        kept.append(RESULT_WITHHELD_NOTE)
    return "\n".join(kept)
