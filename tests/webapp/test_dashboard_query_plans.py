"""The dashboard's two table-wide reads are index-backed, not full scans.

`/api/dashboard` runs on the event loop, so a read that walks every message or
sorts every run row stalls the whole webapp for the length of the scan. The
query plans are asserted (not wall-clock time) so the check is deterministic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.db import store
from src.db.runs import _RUN_LIST_COLUMNS


def _plan(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> str:
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return " | ".join(str(r["detail"]) for r in rows)


def test_backlog_count_uses_the_ingested_at_index(conn: sqlite3.Connection) -> None:
    plan = _plan(
        conn,
        "SELECT COUNT(*) AS n FROM messages WHERE ingested_at > ?",
        ("2026-01-01T00:00:00+00:00",),
    )
    assert "idx_messages_ingested_at" in plan, plan


def test_runs_by_kind_window_uses_the_kind_id_index(conn: sqlite3.Connection) -> None:
    plan = _plan(
        conn,
        f"SELECT {_RUN_LIST_COLUMNS} FROM ("
        f"  SELECT {_RUN_LIST_COLUMNS}, "
        "         ROW_NUMBER() OVER (PARTITION BY kind ORDER BY id DESC) AS rn "
        "  FROM review_runs WHERE kind IN (?, ?)"
        ") WHERE rn <= ? ORDER BY id DESC",
        ("scan", "process", 200),
    )
    assert "idx_review_runs_kind_id" in plan, plan


def test_legacy_review_runs_gain_the_kind_id_index(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE review_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "started_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running');"
    )
    raw.commit()
    raw.close()

    opened = store.connect(db)
    try:
        names = {r["name"] for r in opened.execute("PRAGMA index_list(review_runs)")}
        assert "idx_review_runs_kind_id" in names
    finally:
        opened.close()
