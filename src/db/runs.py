"""Review runs, analysis items/trace, and notification persistence.

Also owns the read-only run/analysis/notification counters
(:func:`count_runs`, :func:`last_run`, :func:`list_review_runs`,
:func:`list_runs_by_kind`, :func:`review_run`, :func:`count_actionable_items`,
:func:`count_notifications_sent`) — grouped here because they all read the
tables this module writes.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.db.connection import _now, _rowid
from src.process_liveness import pid_state

logger = logging.getLogger(__name__)

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
#: Finished, but a chat or a source reported an error along the way.
STATUS_COMPLETED_WITH_ERRORS = "completed_with_errors"
STATUS_FAILED = "failed"
#: A run whose process ended before it recorded a result. Its own terminal
#: state — neither ``failed`` (no error was observed) nor ``completed`` (no result
#: was recorded) — so the Run list stops showing it as in progress without
#: claiming an outcome nobody saw.
STATUS_ABANDONED = "abandoned"

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS, STATUS_FAILED, STATUS_ABANDONED}
)
RUN_STATUSES: frozenset[str] = TERMINAL_STATUSES | {STATUS_RUNNING}

#: The legal moves of a run row: ``running`` ends in exactly one terminal status,
#: and a terminal status never changes again (a late finaliser must not rewrite
#: what an earlier one recorded).
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_RUNNING: TERMINAL_STATUSES,
    **{status: frozenset[str]() for status in TERMINAL_STATUSES},
}

#: The one-shot family checks. They share ``review_runs`` with the message
#: pipeline but never touch the message store, so every list that must treat them
#: apart (Audit scopes, run modal, CLI run capture, staleness) reads this one tuple.
FAMILY_KINDS: tuple[str, ...] = ("traffic-check", "calendar-scan")

#: How long a run of each kind may legitimately stay at ``running``. Only
#: kinds with a known, short bound are listed: a one-shot family check records
#: its result within seconds (slowest observed ~160 s, a Routes call per leg), so
#: a row still ``running`` long past that has no process behind it. A kind absent
#: here (``scan`` / ``process`` can legitimately run for many minutes, and nothing
#: bounds them) is never reconciled by age — only by its owner process being gone.
STALE_RUNNING_AFTER: dict[str, timedelta] = {
    kind: timedelta(minutes=30) for kind in FAMILY_KINDS
}


def start_run(
    conn: sqlite3.Connection,
    mode: str = "review",
    params_json: str | None = None,
    kind: str = "scan",
) -> int:
    """Open a ``running`` row owned by this process.

    ``owner_pid`` is what lets a later process prove the run is dead; a row
    written without one (every row from before the column existed) is never
    judged by it.
    """
    cur = conn.execute(
        "INSERT INTO review_runs (started_at, status, mode, params_json, kind, owner_pid) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), STATUS_RUNNING, mode, params_json, kind, os.getpid()),
    )
    conn.commit()
    return _rowid(cur)


@dataclass(frozen=True)
class RunFunnel:
    """A message run's counters and delivery status, written with its final status."""

    chats_synced: int
    messages_synced: int
    chats_monitored: int
    stage1_passed: int
    stage2_llm_calls: int
    actionable: int
    notification_status: str
    transcriptions: int = 0
    source_funnel_json: str | None = None

    def assignments(self) -> dict[str, object]:
        return {
            "chats_synced": self.chats_synced,
            "messages_synced": self.messages_synced,
            "chats_monitored": self.chats_monitored,
            "stage1_passed": self.stage1_passed,
            "stage2_llm_calls": self.stage2_llm_calls,
            "transcriptions": self.transcriptions,
            "actionable": self.actionable,
            "notification_status": self.notification_status,
            "source_funnel_json": self.source_funnel_json,
        }


def _update_running(conn: sqlite3.Connection, run_id: int, columns: dict[str, object]) -> bool:
    """One ``UPDATE`` of a run row that is still ``running``; True if it matched.

    Column names come from code in this module, never from a caller's input.
    """
    sets = ", ".join(f"{name} = ?" for name in columns)
    cur = conn.execute(
        f"UPDATE review_runs SET {sets} WHERE id = ? AND status = ?",  # noqa: S608
        (*columns.values(), run_id, STATUS_RUNNING),
    )
    conn.commit()
    return cur.rowcount > 0


def finalize_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    *,
    error: str | None = None,
    summary_json: str | None = None,
    chats_reviewed: int | None = None,
    funnel: RunFunnel | None = None,
) -> bool:
    """Move a ``running`` run to ``status``; True if this call moved it.

    The only writer of a terminal status. It matches ``status = 'running'``, so a
    late finaliser can never overwrite what an earlier one recorded, and it writes
    the status, ``completed_at``, error, counters and delivery status in one
    statement, so a crash cannot leave a ``completed`` row without its
    ``notification_status``. ``completed_at`` stays NULL for ``abandoned``: nobody
    observed when (or whether) the run ended, and a timestamp would assert that.
    """
    if status not in LEGAL_TRANSITIONS[STATUS_RUNNING]:
        raise ValueError(f"{status!r} is not a terminal run status")
    columns: dict[str, object] = {
        "status": status,
        "completed_at": None if status == STATUS_ABANDONED else _now(),
        "error": error,
    }
    if summary_json is not None:
        columns["summary_json"] = summary_json
    if chats_reviewed is not None:
        columns["chats_reviewed"] = chats_reviewed
    if funnel is not None:
        columns.update(funnel.assignments())
    if _update_running(conn, run_id, columns):
        return True
    row = conn.execute("SELECT status FROM review_runs WHERE id = ?", (run_id,)).fetchone()
    logger.info(
        "ℹ️ run %s not moved to %s: %s",
        run_id, status, f"already {row['status']}" if row else "no such run",
    )
    return False


def finish_run_summary(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    summary_json: str | None,
    error: str | None = None,
) -> bool:
    """Finalize a run whose outcome is a structured payload, not a funnel.

    The family checks (traffic-check / calendar-scan) record their whole result
    here so a scheduled run is as inspectable as a webapp-launched one.
    """
    return finalize_run(conn, run_id, status, error=error, summary_json=summary_json)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    chats_reviewed: int,
    error: str | None = None,
    *,
    funnel: RunFunnel | None = None,
) -> bool:
    """Finalize a message run; pass ``funnel`` to write its counters in the same commit."""
    return finalize_run(
        conn, run_id, status, error=error, chats_reviewed=chats_reviewed, funnel=funnel
    )


def record_run_funnel(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    chats_synced: int,
    messages_synced: int,
    chats_monitored: int,
    stage1_passed: int,
    stage2_llm_calls: int,
    actionable: int,
    notification_status: str,
    transcriptions: int = 0,
    source_funnel_json: str | None = None,
) -> bool:
    """Snapshot a still-``running`` run's counters ahead of its final status."""
    funnel = RunFunnel(
        chats_synced=chats_synced,
        messages_synced=messages_synced,
        chats_monitored=chats_monitored,
        stage1_passed=stage1_passed,
        stage2_llm_calls=stage2_llm_calls,
        actionable=actionable,
        notification_status=notification_status,
        transcriptions=transcriptions,
        source_funnel_json=source_funnel_json,
    )
    return _update_running(conn, run_id, funnel.assignments())


def abandon_run(conn: sqlite3.Connection, run_id: int, error: str) -> bool:
    """Mark a still-``running`` run abandoned; True if this call changed it."""
    return finalize_run(conn, run_id, STATUS_ABANDONED, error=error)


def _stale_by_age(row: sqlite3.Row, now: datetime) -> str | None:
    """The abandon reason when the row outlived its kind's bound, else None.

    None also covers a kind with no bound and a ``started_at`` that does not
    parse: what the age cannot establish is left alone.
    """
    limit = STALE_RUNNING_AFTER.get(row["kind"])
    if limit is None:
        return None
    try:
        started = datetime.fromisoformat(str(row["started_at"]))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    age = now - started
    if age < limit:
        return None
    return (
        f"abandoned: still running {int(age.total_seconds() // 60)} min after it started "
        f"(limit {int(limit.total_seconds() // 60)} min); "
        "the process ended without recording a result"
    )


def _stale_by_owner(row: sqlite3.Row) -> str | None:
    """The abandon reason when the row's owner process is proven gone, else None.

    A row with no recorded owner, a live owner, or an owner the probe cannot
    classify is left alone: a reused pid keeps a dead run at ``running`` (the old
    behaviour), which is the safe side of the rule.
    """
    owner = row["owner_pid"]
    if owner is None or pid_state(int(owner)) is not False:
        return None
    return f"abandoned: its process (pid {owner}) ended without recording a result"


def reconcile_stale_runs(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[int]:
    """Move runs that can no longer be running to ``abandoned``; returns their ids.

    Two rules, each applied to a ``running`` row. Age: a kind in
    :data:`STALE_RUNNING_AFTER` older than that kind's bound. Owner: any kind
    whose recorded ``owner_pid`` is proven not to be a live process, which is the
    only proof available for ``scan`` / ``process``, whose age establishes nothing.
    Anything neither rule can establish is left exactly as it is. Run it on webapp
    startup and before each run opens its own row, so a killed or restarted
    process is swept up by the next one. One info line per reconciled run.
    """
    now = now or datetime.now(UTC)
    reconciled: list[int] = []
    rows = conn.execute(
        "SELECT id, kind, started_at, owner_pid FROM review_runs "
        "WHERE status = ? ORDER BY id",
        (STATUS_RUNNING,),
    ).fetchall()
    for row in rows:
        reason = _stale_by_age(row, now) or _stale_by_owner(row)
        if reason is None:
            continue
        if abandon_run(conn, int(row["id"]), reason):
            reconciled.append(int(row["id"]))
            logger.info(
                "ℹ️ run %s (%s, started %s) reconciled %s -> %s: %s",
                row["id"], row["kind"], row["started_at"],
                STATUS_RUNNING, STATUS_ABANDONED, reason,
            )
    return reconciled


# The kinds that carry a message-pipeline digest/funnel. Family checks
# share the table but must never be picked up as "the latest digest to deliver"
# or counted as a message scan.
_MESSAGE_KINDS = ("scan", "process")


def latest_run_id(conn: sqlite3.Connection) -> int | None:
    """Return the id of the most recent message-pipeline run, or None."""
    row = conn.execute(
        "SELECT id FROM review_runs WHERE kind IN (?, ?) ORDER BY id DESC LIMIT 1",
        _MESSAGE_KINDS,
    ).fetchone()
    return int(row["id"]) if row else None


def last_run_started_at(conn: sqlite3.Connection, kind: str) -> str | None:
    """Return the ``started_at`` of the most recent **live** run of one ``kind``, or None.

    Backs the CLI's traffic-check cadence self-skip: the Windows job is
    armed at a fixed high frequency and ``wr traffic-check`` compares this
    timestamp against ``traffic.cadence_min`` on every fire, so a cadence edit
    in the UI takes effect without re-arming Task Scheduler. A skipped fire
    never calls :func:`start_run`, so it never shows up here — only real
    checks count as "the last run".

    Excludes ``mode='dry_run'`` rows — a dry run is an explicit,
    on-demand human test pass and must never advance or be counted towards
    the live cadence clock, even though it is recorded in ``review_runs`` like
    any other run.
    """
    row = conn.execute(
        "SELECT started_at FROM review_runs WHERE kind = ? AND mode != 'dry_run' "
        "ORDER BY id DESC LIMIT 1",
        (kind,),
    ).fetchone()
    return str(row["started_at"]) if row else None


def insert_analysis_item(
    conn: sqlite3.Connection,
    run_id: int,
    chat_id: int,
    *,
    action_required: bool,
    priority: str | None,
    summary: str | None,
    suggested_next_action: str | None,
    deadline: str | None,
    confidence: float | None,
    evidence_message_ids_json: str | None,
    deadline_date: str | None = None,
    child: str | None = None,
    task_category: str | None = None,
    prep_complexity: str | None = None,
    calendar_event_id: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO analysis_items
            (run_id, chat_id, action_required, priority, summary,
             suggested_next_action, deadline, deadline_date, confidence,
             evidence_message_ids_json, created_at, child, task_category,
             prep_complexity, calendar_event_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            chat_id,
            1 if action_required else 0,
            priority,
            summary,
            suggested_next_action,
            deadline,
            deadline_date,
            confidence,
            evidence_message_ids_json,
            _now(),
            child,
            task_category,
            prep_complexity,
            calendar_event_id,
        ),
    )
    conn.commit()
    return _rowid(cur)


def set_calendar_event_id(conn: sqlite3.Connection, item_id: int, calendar_event_id: str) -> None:
    """Record the created reminder event id on an already-persisted item."""
    conn.execute(
        "UPDATE analysis_items SET calendar_event_id = ? WHERE id = ?",
        (calendar_event_id, item_id),
    )
    conn.commit()


def find_calendar_event_for_evidence(
    conn: sqlite3.Connection, chat_id: int, evidence_message_ids: list[str]
) -> str | None:
    """Return a prior ``calendar_event_id`` already created for this exact evidence set.

    Identity is the evidence-message-id set, not ``run_id`` — a run is created
    fresh on every scan (including a reprocess replay), but the same underlying
    messages classifying to the same item must never mint a second calendar
    event. An empty evidence list can't be matched, so it always misses
    (the caller creates a fresh event with no dedup guarantee in that rare case).
    """
    if not evidence_message_ids:
        return None
    target = frozenset(evidence_message_ids)
    rows = conn.execute(
        "SELECT evidence_message_ids_json, calendar_event_id FROM analysis_items "
        "WHERE chat_id = ? AND calendar_event_id IS NOT NULL",
        (chat_id,),
    ).fetchall()
    for row in rows:
        if frozenset(json.loads(row["evidence_message_ids_json"] or "[]")) == target:
            return str(row["calendar_event_id"])
    return None


def insert_analysis_trace(
    conn: sqlite3.Connection,
    run_id: int,
    chat_id: int,
    *,
    input_message_ids_json: str,
    input_text: str | None,
    messages_json: str | None,
    stage1_passed: bool,
    stage1_roots_json: str,
    llm_called: bool,
    llm_system_prompt: str | None,
    llm_user_prompt: str | None,
    llm_raw_response: str | None,
    parsed_result_json: str | None,
    final_action: str,
    telegram_text: str | None,
    error: str | None,
    stage1_buckets_json: str = "[]",
) -> int:
    """Persist the full per-chat audit trace for one run (one row per chat)."""
    cur = conn.execute(
        """
        INSERT INTO analysis_trace
            (run_id, chat_id, input_message_ids_json, input_text, messages_json,
             stage1_passed, stage1_roots_json, stage1_buckets_json,
             llm_called, llm_system_prompt,
             llm_user_prompt, llm_raw_response, parsed_result_json, final_action,
             telegram_text, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            chat_id,
            input_message_ids_json,
            input_text,
            messages_json,
            1 if stage1_passed else 0,
            stage1_roots_json,
            stage1_buckets_json,
            1 if llm_called else 0,
            llm_system_prompt,
            llm_user_prompt,
            llm_raw_response,
            parsed_result_json,
            final_action,
            telegram_text,
            error,
            _now(),
        ),
    )
    conn.commit()
    return _rowid(cur)


def traces_for_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    """Return a run's audit-trace rows joined to chat names, ordered by chat."""
    return list(
        conn.execute(
            "SELECT t.*, c.display_name, c.source FROM analysis_trace t "
            "JOIN chats c ON c.id = t.chat_id "
            "WHERE t.run_id = ? ORDER BY t.chat_id",
            (run_id,),
        ).fetchall()
    )


def list_filtered_traces(
    conn: sqlite3.Connection,
    since: str,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Recent non-actionable trace rows across runs, newest first."""
    return list(
        conn.execute(
            "SELECT t.id AS trace_id, t.run_id, t.created_at, t.stage1_passed, "
            "t.stage1_roots_json, t.llm_called, t.parsed_result_json, t.final_action, "
            "c.display_name, c.source "
            "FROM analysis_trace t JOIN chats c ON c.id = t.chat_id "
            "WHERE t.final_action != 'actionable' AND t.created_at >= ? "
            "ORDER BY t.created_at DESC, t.id DESC LIMIT ? OFFSET ?",
            (since, max(1, limit), max(0, offset)),
        ).fetchall()
    )


def count_filtered_traces(conn: sqlite3.Connection, since: str) -> int:
    """Count recent non-actionable trace rows for bounded API pagination."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM analysis_trace "
        "WHERE final_action != 'actionable' AND created_at >= ?",
        (since,),
    ).fetchone()
    return int(row["n"])


def actionable_items_for_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT ai.*, c.display_name FROM analysis_items ai "
            "JOIN chats c ON c.id = ai.chat_id "
            "WHERE ai.run_id = ? AND ai.action_required = 1 "
            "ORDER BY ai.chat_id, ai.id",
            (run_id,),
        ).fetchall()
    )


def record_notification(
    conn: sqlite3.Connection, run_id: int, channel: str, status: str, error: str | None = None
) -> int:
    sent_at = _now() if status == "sent" else None
    cur = conn.execute(
        "INSERT INTO notifications (run_id, channel, status, sent_at, error) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, channel, status, sent_at, error),
    )
    conn.commit()
    return _rowid(cur)


def count_runs(conn: sqlite3.Connection) -> int:
    """Number of message-pipeline (scan/process) runs recorded."""
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM review_runs WHERE kind IN (?, ?)",
            _MESSAGE_KINDS,
        ).fetchone()["n"]
    )


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent message-pipeline run row, or None if none has happened."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM review_runs WHERE kind IN (?, ?) ORDER BY id DESC LIMIT 1",
        _MESSAGE_KINDS,
    ).fetchone()
    return row


#: Newest runs of *each kind* the "last run of kind X" surfaces read — the
#: Dashboard activity cards, the Family tab's last check / sweep, the Sources
#: health calendar row. The bound is per kind, never global: a kind that runs
#: every few minutes (``traffic-check``) must not push a once-a-day kind
#: (``calendar-scan``) out of the window. It equals the filesystem record cap
#: (:data:`app.webapp.runs.RETENTION_PER_KIND`), so the DB read never looks
#: further back than retention keeps run output.
RUNS_PER_KIND = 200

_RUN_LIST_COLUMNS = (
    "id, kind, started_at, completed_at, status, mode, params_json, "
    "summary_json, chats_synced, messages_synced, chats_monitored, chats_reviewed, "
    "stage1_passed, stage2_llm_calls, transcriptions, actionable, "
    "notification_status, source_funnel_json, error"
)


def list_review_runs(
    conn: sqlite3.Connection,
    limit: int = 50,
    *,
    kinds: Sequence[str] | None = None,
    exclude_kinds: Sequence[str] | None = None,
    before_id: int | None = None,
) -> list[sqlite3.Row]:
    """Review/scan runs newest-first, with the funnel columns the Audit list needs.

    Read-only (SELECT only) so it is safe on the webapp request path; powers the
    Activity tab's run list where each run is shown with its mode, parameters, and
    funnel counters before drilling into the per-chat trace.

    ``kinds`` / ``exclude_kinds`` narrow the query in SQL *before* the ``LIMIT``:
    traffic checks are ~94% of runs, so filtering the newest ``limit``
    rows afterwards would leave a Messages list nearly empty. ``before_id`` is an
    exclusive id cursor for paging older rows.
    """
    where: list[str] = []
    args: list[object] = []
    if kinds:
        where.append(f"kind IN ({','.join('?' for _ in kinds)})")
        args.extend(kinds)
    if exclude_kinds:
        where.append(f"kind NOT IN ({','.join('?' for _ in exclude_kinds)})")
        args.extend(exclude_kinds)
    if before_id is not None:
        where.append("id < ?")
        args.append(before_id)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    return list(
        conn.execute(
            f"SELECT {_RUN_LIST_COLUMNS} FROM review_runs{clause} ORDER BY id DESC LIMIT ?",
            (*args, max(1, limit)),
        ).fetchall()
    )


def list_runs_by_kind(
    conn: sqlite3.Connection,
    kinds: Sequence[str],
    *,
    per_kind: int = RUNS_PER_KIND,
) -> list[sqlite3.Row]:
    """The newest ``per_kind`` runs of each of ``kinds``, newest-first overall.

    Same columns as :func:`list_review_runs`. Unlike a global ``LIMIT`` followed
    by a Python filter, the window is applied *per kind* in SQL, so a chatty kind
    cannot crowd a rare one out of the result (see :data:`RUNS_PER_KIND`). Read-only.
    """
    if not kinds:
        return []
    placeholders = ",".join("?" for _ in kinds)
    return list(
        conn.execute(
            f"SELECT {_RUN_LIST_COLUMNS} FROM ("
            f"  SELECT {_RUN_LIST_COLUMNS}, "
            "         ROW_NUMBER() OVER (PARTITION BY kind ORDER BY id DESC) AS rn "
            f"  FROM review_runs WHERE kind IN ({placeholders})"
            ") WHERE rn <= ? ORDER BY id DESC",
            (*kinds, max(1, per_kind)),
        ).fetchall()
    )


def list_live_scan_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Live scans oldest-first with only the fields needed for coverage gaps."""
    return list(
        conn.execute(
            "SELECT id, started_at, status, notification_status "
            "FROM review_runs WHERE kind = 'scan' AND mode = 'live' "
            "ORDER BY started_at, id"
        ).fetchall()
    )


def review_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    """A single review run by id (full row), or None if it doesn't exist."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM review_runs WHERE id = ?", (run_id,)
    ).fetchone()
    return row


def count_actionable_items(conn: sqlite3.Connection) -> int:
    """Total actionable analysis verdicts across all runs (real alerts raised)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM analysis_items WHERE action_required = 1"
    ).fetchone()
    return int(row["n"])


def count_notifications_sent(conn: sqlite3.Connection) -> int:
    """Total notifications successfully delivered across all runs."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM notifications WHERE status = 'sent'"
    ).fetchone()
    return int(row["n"])
