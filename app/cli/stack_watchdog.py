"""Stack dump of a CLI process that outlives its expected duration.

A scheduled run that stalls — a slow filesystem, a network call nobody answers,
an interpreter shutdown that never finishes — is killed by App Launcher's Job
timeout with no traceback, and the cause dies with it. Arming
:func:`faulthandler.dump_traceback_later` makes the interpreter itself write
every thread's stack to the real stderr (the stream App Launcher captures into
the Job log) once the process passes its budget, and again every budget after
that. It runs on a C-level timer thread, so it fires even when the stuck Python
code never yields, and it covers the import phase and interpreter shutdown,
not only the command body.

This module imports only the standard library: the entry points arm it before
importing the rest of the app, so a stall inside those imports is caught too.
Stack frames carry file, line and function names only — never local values, so
no message content can reach the Job log through a dump.
"""

from __future__ import annotations

import faulthandler
import sys
from typing import TextIO

#: Seconds before the first dump, per verb. A family check normally finishes in
#: under 6 s against a 5-minute Job timeout, so a minute is already a stall and
#: leaves room for several dumps before the kill.
_VERB_BUDGET_S = {
    "traffic-check": 60.0,
    "calendar-scan": 60.0,
}
#: Everything else, sized for a scan: it legitimately runs for minutes against
#: a 20-minute Job timeout, so it should dump only when it is clearly stuck.
_DEFAULT_BUDGET_S = 900.0

_armed_budget: float | None = None


def budget_for(verb: str | None) -> float:
    """Seconds a ``verb`` may run before its first stack dump."""
    return _VERB_BUDGET_S.get(verb or "", _DEFAULT_BUDGET_S)


def arm(verb: str | None, stream: TextIO | None = None) -> float | None:
    """Start the dump timer for this process; return its budget, or None if off.

    ``stream`` defaults to the process's original stderr. Off (``None``) when
    there is no real file descriptor to write to — ``pythonw`` has no stderr, and
    a dump into nowhere would only hide that the watchdog is not running.
    """
    global _armed_budget
    target = sys.__stderr__ if stream is None else stream
    if target is None:
        return None
    try:
        target.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    budget = budget_for(verb)
    faulthandler.dump_traceback_later(budget, repeat=True, file=target)
    _armed_budget = budget
    return budget


def arm_for_argv(argv: list[str]) -> float | None:
    """:func:`arm` for a ``launcher.py``-style argv (the verb is ``argv[1]``)."""
    # The tray is a long-lived surface, not a run; a dump every few minutes of
    # it idling would be noise, never a diagnosis.
    verb = argv[1] if len(argv) > 1 else None
    if verb == "tray":
        return None
    return arm(verb)


def armed_budget() -> float | None:
    """The budget the timer was armed with in this process, or None."""
    return _armed_budget
