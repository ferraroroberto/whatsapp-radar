"""The progress-sink contract, shared by every surface that streams a run.

A long ``scan`` / ``resync`` is indistinguishable from a hang unless it says
what it is doing, so the CLI and the Execution tab hand the pipeline a sink that
writes one human-readable line per stage. The contract is deliberately
string-in/``None``-out: a sink can never affect control flow, and omitting one
(tests, library callers) changes nothing but the silence.

This lives at the top of ``src`` rather than inside ``src/analysis`` because all
three layers emit progress — ``src.analysis`` (pipeline, review, transcription),
``src.connector`` (preflight) and ``src.db`` (sync) — and the lower two must not
import the analysis package to say so (#294). Dependency-free by design, exactly
like :mod:`src.paths`.
"""

from __future__ import annotations

from collections.abc import Callable

#: A sink for human-readable progress lines, or ``None`` for a silent run.
Progress = Callable[[str], None]


def emit(progress: Progress | None, line: str) -> None:
    """Write one progress line, or do nothing when no sink is wired."""
    if progress is not None:
        progress(line)
