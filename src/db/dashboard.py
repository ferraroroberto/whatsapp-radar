"""Cross-table, read-only aggregates powering the Dashboard and Chats & Config
tabs (#9, #10). Every function here only ever SELECTs — no writes, no cursor
changes — so all are safe to call from the webapp request path. Single-table
reads/counts (chat, message, run, notification) live with their owning entity
module instead; this module is only for the genuinely cross-table assemblies.
"""

from __future__ import annotations

import sqlite3


def count_chats_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """Chat counts keyed by status, always including the three known statuses."""
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM chats GROUP BY status").fetchall()
    counts = {row["status"]: int(row["n"]) for row in rows}
    return {
        "discovered": counts.get("discovered", 0),
        "monitored": counts.get("monitored", 0),
        "ignored": counts.get("ignored", 0),
    }


def messages_per_chat(
    conn: sqlite3.Connection, *, monitored_only: bool = True
) -> list[sqlite3.Row]:
    """Per-chat message counts (id, display_name, status, last_message_at, message_count).

    Most-active chats first. ``monitored_only`` restricts to chats being watched,
    which is what the Dashboard's per-channel table shows. Linked child chats are
    excluded from the monitored view so a family that is one person isn't listed
    twice; the child's messages remain on its own row in the all-chats view.

    The count and last-message time are computed over the chat's whole **family**
    — itself plus any linked children — so a monitored parent's row represents the
    merged family (matching ``chats_overview`` and the Chats tab), not just its own
    messages. For an unlinked/standalone chat the family is just itself, so counts
    and ordering are unchanged.
    """
    where = (
        "WHERE c.status = 'monitored' AND c.parent_chat_id IS NULL" if monitored_only else ""
    )
    family = (
        "m.chat_id IN (SELECT x.id FROM chats x WHERE x.id = c.id OR x.parent_chat_id = c.id)"
    )
    return list(
        conn.execute(
            "SELECT c.id, c.display_name, c.status, "
            f"(SELECT MAX(m.message_timestamp) FROM messages m WHERE {family}) AS last_message_at, "
            f"(SELECT COUNT(*) FROM messages m WHERE {family}) AS message_count "
            f"FROM chats c {where} "
            "ORDER BY last_message_at IS NULL, last_message_at DESC, c.id"
        ).fetchall()
    )


def chats_overview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All chats with their status, message count, and latest message preview.

    Columns: id, source_chat_id, display_name, chat_type, status,
    last_message_at, message_count, last_message_text. The count, latest time, and
    preview are computed over the chat's whole **family** — itself plus any linked
    children — so a parent row represents the merged family, not just its own
    messages (a child's newer message correctly floats the parent to the top and
    sets the preview). The family set is ``c`` plus chats whose ``parent_chat_id``
    is ``c``; for a standalone or child chat that is just itself. Most recently
    active first (NULLs last) so the operator's live chats surface at the top.
    """
    family = (
        "m.chat_id IN (SELECT x.id FROM chats x WHERE x.id = c.id OR x.parent_chat_id = c.id)"
    )
    return list(
        conn.execute(
            "SELECT c.id, c.source, c.source_chat_id, c.display_name, c.alias, c.chat_type, "
            "c.status, c.parent_chat_id, "
            f"(SELECT COUNT(*) FROM messages m WHERE {family}) AS message_count, "
            f"(SELECT MAX(m.message_timestamp) FROM messages m WHERE {family}) AS last_message_at, "
            f"(SELECT m.text FROM messages m WHERE {family} "
            " ORDER BY m.message_timestamp DESC, m.id DESC LIMIT 1) AS last_message_text "
            "FROM chats c "
            "ORDER BY last_message_at IS NULL, last_message_at DESC, c.id"
        ).fetchall()
    )
