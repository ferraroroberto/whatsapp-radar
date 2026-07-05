"""Reprocess (full cache rebuild) snapshot + wipe primitives.

The local store is a cache rebuildable from the connector buffer. Reprocess
(:mod:`src.db.reprocess`) snapshots operator-set state, wipes the derived
cache, re-ingests with current reader logic, then re-applies the snapshot.
These two helpers are the snapshot + wipe primitives; the orchestration lives
in ``reprocess.py`` so the SQL stays here with the schema knowledge.
"""

from __future__ import annotations

import sqlite3


def snapshot_operator_state(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Operator-set state worth preserving across a rebuild: status, alias, link.

    Returns (source_chat_id, status, alias, parent_source_chat_id) for every chat
    the operator has touched — anything not in the default 'discovered'/no-alias/
    unlinked resting state. The parent is captured by *its* ``source_chat_id`` (not
    internal id, which the rebuild reassigns) so the parent↔child link can be
    re-resolved after re-ingest. A linked child with otherwise-default state is
    still included because of its ``parent_chat_id``.
    """
    return list(
        conn.execute(
            "SELECT c.source_chat_id, c.status, c.alias, "
            "p.source_chat_id AS parent_source_chat_id "
            "FROM chats c LEFT JOIN chats p ON p.id = c.parent_chat_id "
            "WHERE c.status != 'discovered' OR c.alias IS NOT NULL "
            "OR c.parent_chat_id IS NOT NULL"
        ).fetchall()
    )


def clear_all_data(conn: sqlite3.Connection) -> None:
    """Wipe every derived/cache table so a reprocess can rebuild from scratch.

    Deletes children before parents so the wipe holds whether or not SQLite's
    per-connection foreign-key enforcement happens to be on. Run/analysis history
    is intentionally discarded — it cannot be re-keyed to the rebuilt chat ids.
    """
    for table in (
        "notifications",
        "analysis_items",
        "analysis_trace",
        "chat_review_state",
        "messages",
        "review_runs",
        "chats",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
