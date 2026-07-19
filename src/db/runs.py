"""Review runs, analysis items/trace, and notification persistence.

Also owns the read-only run/analysis/notification counters that originated
under the old "dashboard aggregates" and "chats & config tab" banners
(:func:`count_runs`, :func:`last_run`, :func:`list_review_runs`,
:func:`review_run`, :func:`count_actionable_items`,
:func:`count_notifications_sent`) — grouped here because they all read the
tables this module writes.
"""

from __future__ import annotations

import sqlite3

from src.db.connection import _now, _rowid


def start_run(
    conn: sqlite3.Connection,
    mode: str = "review",
    params_json: str | None = None,
    kind: str = "scan",
) -> int:
    cur = conn.execute(
        "INSERT INTO review_runs (started_at, status, mode, params_json, kind) "
        "VALUES (?, 'running', ?, ?, ?)",
        (_now(), mode, params_json, kind),
    )
    conn.commit()
    return _rowid(cur)


def finish_run_summary(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    summary_json: str | None,
    error: str | None = None,
) -> None:
    """Finalize a run whose outcome is a structured payload, not a funnel.

    The family checks (traffic-check / calendar-scan) record their whole result
    here so a scheduled run is as inspectable as a webapp-launched one (#163).
    """
    conn.execute(
        "UPDATE review_runs SET completed_at = ?, status = ?, summary_json = ?, error = ? "
        "WHERE id = ?",
        (_now(), status, summary_json, error, run_id),
    )
    conn.commit()


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
) -> None:
    """Persist a run's funnel counters and final notification status."""
    conn.execute(
        "UPDATE review_runs SET chats_synced = ?, messages_synced = ?, chats_monitored = ?, "
        "stage1_passed = ?, stage2_llm_calls = ?, transcriptions = ?, actionable = ?, "
        "notification_status = ?, source_funnel_json = ? WHERE id = ?",
        (
            chats_synced,
            messages_synced,
            chats_monitored,
            stage1_passed,
            stage2_llm_calls,
            transcriptions,
            actionable,
            notification_status,
            source_funnel_json,
            run_id,
        ),
    )
    conn.commit()


# The kinds that carry a message-pipeline digest/funnel. Family checks (#163)
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


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    chats_reviewed: int,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE review_runs SET completed_at = ?, status = ?, chats_reviewed = ?, error = ? "
        "WHERE id = ?",
        (_now(), status, chats_reviewed, error, run_id),
    )
    conn.commit()


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
) -> int:
    cur = conn.execute(
        """
        INSERT INTO analysis_items
            (run_id, chat_id, action_required, priority, summary,
             suggested_next_action, deadline, deadline_date, confidence,
             evidence_message_ids_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ),
    )
    conn.commit()
    return _rowid(cur)


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


def list_review_runs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Review/scan runs newest-first, with the funnel columns the Audit list needs.

    Read-only (SELECT only) so it is safe on the webapp request path; powers the
    Audit tab's run list where each run is shown with its mode, parameters, and
    funnel counters before drilling into the per-chat trace.
    """
    return list(
        conn.execute(
            "SELECT id, kind, started_at, completed_at, status, mode, params_json, "
            "summary_json, chats_synced, messages_synced, chats_monitored, chats_reviewed, "
            "stage1_passed, stage2_llm_calls, transcriptions, actionable, "
            "notification_status, source_funnel_json, error "
            "FROM review_runs ORDER BY id DESC LIMIT ?",
            (max(1, limit),),
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
