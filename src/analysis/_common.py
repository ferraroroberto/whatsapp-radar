"""Shared analysis-pipeline primitives.

Small helpers used by more than one pipeline module (the scan pipeline and the
transcription phase). Kept here so the progress-sink contract has a single
definition rather than being copy-pasted per module.
"""

from __future__ import annotations

from collections.abc import Callable

# A sink for human-readable progress lines. The CLI wires it to stdout so a
# launched run streams its funnel as it happens; tests/library callers may omit
# it. Kept deliberately string-in/None-out so it can't affect control flow.
Progress = Callable[[str], None]


def _emit(progress: Progress | None, line: str) -> None:
    if progress is not None:
        progress(line)
