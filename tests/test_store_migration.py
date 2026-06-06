"""Opening a pre-#7 database backfills the review_runs funnel columns.

A DB created by the original spike has a slimmer review_runs table. store.connect
must migrate it additively so the Dashboard (and #7's funnel) can read it without
crashing — and without losing the existing rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.db import store

_LEGACY_REVIEW_RUNS = """
CREATE TABLE review_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    completed_at   TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    chats_reviewed INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);
"""


def test_connect_migrates_legacy_review_runs(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(_LEGACY_REVIEW_RUNS)
    raw.execute(
        "INSERT INTO review_runs (started_at, status, chats_reviewed) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'completed', 2)"
    )
    raw.commit()
    raw.close()

    conn = store.connect(db)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(review_runs)")}
        assert {"mode", "messages_synced", "actionable", "notification_status"} <= cols

        # The pre-existing row survives and reads with sane defaults.
        assert store.count_runs(conn) == 1
        last = store.last_run(conn)
        assert last is not None
        assert last["mode"] == "review"  # column default applied to the old row
        assert last["actionable"] == 0

        # Idempotent: a second open is a no-op.
        conn.close()
        conn2 = store.connect(db)
        assert store.count_runs(conn2) == 1
        conn2.close()
    finally:
        conn.close()
