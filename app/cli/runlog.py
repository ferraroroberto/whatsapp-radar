"""Run-output capture for CLI- and Jobs-launched runs (#233).

The Execution tab's run viewer only ever showed output for runs the *webapp*
spawned: :func:`app.webapp.runs.start_run` opens ``output.log`` and hands it to
the child as stdout/stderr. A run fired from a terminal — or by an App Launcher
Job, which is how the scheduled ``traffic-check`` / ``calendar-scan`` / ``scan``
actually run — left the viewer showing a "no captured output" placeholder. The
*outcome* was recorded; the *reason* was not, so a check reporting
``0 checked · 0 alerts`` was unexplainable from the radar's own admin UI.

This closes the gap from the other side: the CLI writes the same filesystem run
record the webapp does, teeing its own output into it. Because the record
carries the DB run id parsed from the ``__WR_RESULT__`` sentinel, the merge in
:func:`app.webapp.routers.execution.list_execution_runs` unifies it with the DB
row into one entry — no new viewer path and no client-side change.

Two deliberate choices:

* **A tee, not a redirect.** The real stream is written first and unmodified, so
  App Launcher's own job-log capture stays byte-for-byte what it was and a long
  ``scan``'s progress still streams live to both sinks.
* **Best-effort.** An unwritable runs directory, a full disk, a failed write —
  none of it may break the command being run. Capture is observability; the run
  is the point.

Only :data:`LAUNCHABLE_VERBS` are captured. ``status``/``chats``/``monitor``/
``ignore``/``tray`` are interactive or long-lived and record no run, so giving
them run directories would be noise.
"""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import IO, TextIO, cast

from app.webapp import runs

#: Verbs that record a run and are worth a run record. The webapp exposes the
#: same set as Execution actions; the inverse mapping (action → argv tail) lives
#: in ``app/webapp/routers/execution.py::_compose_argv``.
LAUNCHABLE_VERBS = frozenset(
    {"scan", "review", "resync", "reprocess", "notify", "calendar-scan", "traffic-check"}
)

#: The webapp calls the ``review`` verb's action "process". The run record must
#: use the webapp's name, or the same execution would file under two kinds and
#: the viewer's per-kind lookup would miss it.
_VERB_KINDS = {"review": "process"}


def run_kind(verb: str) -> str:
    """The run-record ``kind`` for a CLI verb."""
    return _VERB_KINDS.get(verb, verb)


class _Tee:
    """Write to the real stream *and* the run log — real stream first.

    Attribute lookups that aren't ``write``/``flush`` fall through to the real
    stream, so anything probing ``encoding``, ``isatty()`` or ``fileno()`` sees
    the console this process actually writes to.
    """

    def __init__(self, stream: TextIO, log: IO[bytes]) -> None:
        self._stream = stream
        self._log = log

    def write(self, data: str) -> int:
        written = self._stream.write(data)
        try:
            self._log.write(data.encode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass  # a broken log must never break the command
        return written

    def flush(self) -> None:
        self._stream.flush()
        try:
            self._log.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def _open_record(verb: str, argv: list[str]) -> tuple[Path, IO[bytes]] | None:
    """Create the run directory + open its log, or None if that isn't possible."""
    kind = run_kind(verb)
    try:
        run_dir = runs.new_run_dir(kind, runs.new_run_id())
        log: IO[bytes] = (run_dir / "output.log").open("wb")
    except OSError:
        return None
    runs.write_run_json(
        run_dir,
        kind=kind,
        run_id=run_dir.name,
        status="running",
        started_at=runs.now_iso(),
        argv=argv,
        origin="cli",
        pid=os.getpid(),
    )
    return run_dir, log


def run_captured(verb: str, argv: list[str], command: Callable[[], int]) -> int:
    """Run ``command``, teeing this process's output into a run record.

    Passes straight through — no record, no tee — when the verb records no run
    or when the webapp is already capturing this process
    (:data:`app.webapp.runs.CAPTURED_ENV_VAR`).
    """
    if verb not in LAUNCHABLE_VERBS or os.environ.get(runs.CAPTURED_ENV_VAR):
        return command()
    record = _open_record(verb, argv)
    if record is None:
        return command()
    run_dir, log = record

    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = cast(TextIO, _Tee(real_out, log))
    sys.stderr = cast(TextIO, _Tee(real_err, log))
    exit_code: int | None = None
    try:
        exit_code = command()
        return exit_code
    except BaseException:
        # Format the traceback into the log *while the tee is still installed*.
        # Once the streams are restored the interpreter would print it to the
        # real stderr only, leaving a failed run record with no reason in it —
        # exactly the blindness this module exists to remove.
        sys.stderr.write(traceback.format_exc())
        raise
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        try:
            log.close()
        except OSError:
            pass
        runs.finalize_record(run_dir, exit_code)
