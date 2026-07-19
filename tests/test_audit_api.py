"""Audit tab (#12): store run-list queries + the /api/audit endpoints.

A hand-seeded fixture DB (sanitized names only) gives known numbers; the endpoint
tests check the JSON shape (run list with funnel, per-chat trace drill-down) and
the bearer gate. Everything is offline — no connector, no LLM, no network.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from starlette.testclient import TestClient

from app.webapp.server import create_app
from src.db import store
from src.models import ChatRecord, MessageRecord
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)
REMOTE = ("203.0.113.5", 5555)


def _seed(conn: sqlite3.Connection) -> int:
    """One live run over two monitored chats: one actionable (LLM called), one
    filtered at Stage 1. Returns the run id. Plus a resync sync_log marker."""
    a = store.upsert_chat(
        conn,
        ChatRecord(
            source_chat_id="g1",
            display_name="Class 4A Group",
            chat_type="email",
            source="gmail",
        ),
    )
    b = store.upsert_chat(
        conn,
        ChatRecord(source_chat_id="g2", display_name="School Parents Group", chat_type="group"),
    )
    store.set_chat_status(conn, a, "monitored")
    store.set_chat_status(conn, b, "monitored")
    for i in range(2):
        store.insert_message(
            conn,
            a,
            MessageRecord(
                source_message_id=f"a{i}",
                message_timestamp=f"2026-06-01T10:0{i}:00+00:00",
                text="bring a packed lunch tomorrow",
                sender_label="Parent",
            ),
        )

    run_id = store.start_run(conn, mode="live", params_json=json.dumps({"days": 7}))
    store.record_run_funnel(
        conn,
        run_id,
        chats_synced=2,
        messages_synced=2,
        chats_monitored=2,
        stage1_passed=1,
        stage2_llm_calls=1,
        actionable=1,
        notification_status="sent",
        source_funnel_json=json.dumps(
            {
                "gmail": {
                    "sync_status": "success",
                    "messages_synced": 2,
                    "monitored_channels": 1,
                    "messages_checked": 2,
                    "stage1_passed": 1,
                    "stage1_rejected": 0,
                    "llm_calls": 1,
                    "actionable": 1,
                    "cursors_advanced": 1,
                }
            }
        ),
    )
    store.finish_run(conn, run_id, "completed", chats_reviewed=2)

    # Chat A: passed Stage 1, LLM called, actionable verdict + delivered text.
    store.insert_analysis_trace(
        conn,
        run_id,
        a,
        input_message_ids_json=json.dumps(["a0", "a1"]),
        input_text="bring a packed lunch tomorrow",
        messages_json=json.dumps(
            [
                {"id": "a0", "sender": "Parent", "text": "bring a packed lunch",
                 "roots": ["lunch"], "buckets": ["preparation"]},
                {"id": "a1", "sender": "Parent", "text": "tomorrow please",
                 "roots": ["tomorrow"], "buckets": ["deadline"]},
            ]
        ),
        stage1_passed=True,
        stage1_roots_json=json.dumps(["lunch", "tomorrow"]),
        stage1_buckets_json=json.dumps(["preparation", "deadline"]),
        llm_called=True,
        llm_system_prompt="You are a classifier.",
        llm_user_prompt="Messages: bring a packed lunch tomorrow",
        llm_raw_response='{"action_required": true, "priority": "high"}',
        parsed_result_json=json.dumps(
            {"action_required": True, "priority": "high", "evidence_message_ids": ["a0"]}
        ),
        final_action="actionable",
        telegram_text="📌 Class 4A Group: packed lunch tomorrow",
        error=None,
    )
    # Chat B: filtered at Stage 1, no LLM call.
    store.insert_analysis_trace(
        conn,
        run_id,
        b,
        input_message_ids_json=json.dumps(["b0"]),
        input_text="thanks everyone",
        messages_json=json.dumps(
            [{"id": "b0", "sender": "Parent", "text": "thanks everyone", "roots": []}]
        ),
        stage1_passed=False,
        stage1_roots_json=json.dumps([]),
        llm_called=False,
        llm_system_prompt=None,
        llm_user_prompt=None,
        llm_raw_response=None,
        parsed_result_json=None,
        final_action="not_actionable",
        telegram_text=None,
        error=None,
    )

    store.record_sync(
        conn,
        source="resync",
        chats_added=1,
        chats_updated=0,
        messages_added=3,
    )
    return run_id


# --- store queries ----------------------------------------------------------

def test_list_review_runs_newest_first(conn: sqlite3.Connection) -> None:
    first = store.start_run(conn, mode="dry_run")
    store.finish_run(conn, first, "completed", chats_reviewed=0)
    second = store.start_run(conn, mode="live")

    runs = store.list_review_runs(conn, 10)
    assert [r["id"] for r in runs] == [second, first]
    assert runs[0]["mode"] == "live"


def test_review_run_by_id(conn: sqlite3.Connection) -> None:
    run_id = _seed(conn)
    row = store.review_run(conn, run_id)
    assert row is not None and int(row["id"]) == run_id
    assert store.review_run(conn, 9999) is None


# --- endpoints --------------------------------------------------------------

def _client(db: Path) -> TestClient:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    return TestClient(app, client=LOOPBACK)


def test_audit_runs_list_shape(tmp_path: Path) -> None:
    db = tmp_path / "audit.sqlite3"
    conn = store.connect(db)
    run_id = _seed(conn)
    conn.close()

    with _client(db) as client:
        body = client.get("/api/audit/runs").json()

    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["id"] == run_id
    assert run["mode"] == "live"
    assert run["params"] == {"days": 7}
    assert run["funnel"]["stage1_passed"] == 1
    assert run["funnel"]["actionable"] == 1
    assert run["notification_status"] == "sent"
    assert run["sources"]["gmail"]["llm_calls"] == 1

    # The resync maintenance marker is surfaced; scan-sourced syncs would not be.
    assert len(body["syncs"]) == 1
    assert body["syncs"][0]["source"] == "resync"
    assert body["syncs"][0]["messages_added"] == 3


def test_audit_groups_contiguous_offline_scans_into_coverage_gap(tmp_path: Path) -> None:
    db = tmp_path / "coverage.sqlite3"
    conn = store.connect(db)

    def add_scan(started_at: str, *, offline: bool) -> int:
        run_id = store.start_run(conn, mode="live", kind="scan")
        store.record_run_funnel(
            conn,
            run_id,
            chats_synced=0,
            messages_synced=0,
            chats_monitored=0,
            stage1_passed=0,
            stage2_llm_calls=0,
            actionable=0,
            notification_status="offline" if offline else "none",
        )
        store.finish_run(
            conn,
            run_id,
            "failed" if offline else "completed",
            chats_reviewed=0,
        )
        conn.execute(
            "UPDATE review_runs SET started_at = ?, completed_at = ? WHERE id = ?",
            (started_at, started_at, run_id),
        )
        conn.commit()
        return run_id

    add_scan("2026-06-19T18:00:00+00:00", offline=False)
    gap_ids = [
        add_scan("2026-06-20T18:00:00+00:00", offline=True),
        add_scan("2026-06-21T18:00:00+00:00", offline=True),
        add_scan("2026-06-25T18:00:00+00:00", offline=True),
    ]
    recovery_id = add_scan("2026-06-26T18:00:00+00:00", offline=False)
    add_scan("2026-06-27T18:00:00+00:00", offline=True)
    add_scan("2026-06-28T18:00:00+00:00", offline=False)
    conn.close()

    with _client(db) as client:
        body = client.get("/api/audit/runs").json()

    assert body["coverage_gaps"] == [
        {
            "started_at": "2026-06-20T18:00:00+00:00",
            "ended_at": "2026-06-25T18:00:00+00:00",
            "duration_days": 5,
            "failed_runs": 3,
            "run_ids": gap_ids,
            "recovered_at": "2026-06-26T18:00:00+00:00",
            "recovery_run_id": recovery_id,
        }
    ]


def test_audit_run_drilldown_trace(tmp_path: Path) -> None:
    db = tmp_path / "audit.sqlite3"
    conn = store.connect(db)
    run_id = _seed(conn)
    conn.close()

    with _client(db) as client:
        body = client.get(f"/api/audit/runs/{run_id}").json()

    assert body["run"]["id"] == run_id
    traces = {t["display_name"]: t for t in body["traces"]}
    assert set(traces) == {"Class 4A Group", "School Parents Group"}

    a = traces["Class 4A Group"]
    assert a["final_action"] == "actionable"
    assert a["source"] == "gmail"
    assert a["stage1_passed"] is True
    assert a["stage1_roots"] == ["lunch", "tomorrow"]
    assert a["stage1_buckets"] == ["preparation", "deadline"]
    assert a["llm_called"] is True
    assert a["llm_system_prompt"] == "You are a classifier."
    assert "packed lunch" in a["llm_user_prompt"]
    assert a["parsed_result"]["action_required"] is True
    assert a["telegram_text"] is not None
    # Per-message breakdown (#12): each message carries its own Stage-1 roots, and
    # the LLM-flagged message is identifiable via parsed_result.evidence_message_ids.
    assert [m["id"] for m in a["messages"]] == ["a0", "a1"]
    assert a["messages"][0]["roots"] == ["lunch"]
    assert a["parsed_result"]["evidence_message_ids"] == ["a0"]

    b = traces["School Parents Group"]
    assert b["final_action"] == "not_actionable"
    assert b["stage1_passed"] is False
    assert b["llm_called"] is False
    assert b["llm_system_prompt"] is None
    # A filtered message still appears, with no matched roots.
    assert b["messages"] == [
        {"id": "b0", "sender": "Parent", "text": "thanks everyone", "roots": []}
    ]


def test_audit_run_unknown_404(tmp_path: Path) -> None:
    db = tmp_path / "audit.sqlite3"
    store.connect(db).close()
    with _client(db) as client:
        assert client.get("/api/audit/runs/4242").status_code == 404


def test_audit_requires_token_from_remote(tmp_path: Path) -> None:
    db = tmp_path / "gated.sqlite3"
    store.connect(db).close()

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="secret")
    app.state.db_path = db
    with TestClient(app, client=REMOTE) as client:
        assert client.get("/api/audit/runs").status_code == 401
        ok = client.get("/api/audit/runs", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
