"""Chat CRUD, status, aliasing, and parent↔child linking.

Also owns the two basic chat-entity reads (:func:`get_chat`, :func:`count_chats`)
that originated under the old "chats & config tab" banner in the pre-split
``store.py`` — they're chat primitives other modules (``dashboard``,
``sync_log``, :func:`link_chats` below) need, so they live with the entity
rather than with the tab that first consumed them.
"""

from __future__ import annotations

import sqlite3

from src.db.connection import _now
from src.models import ChatRecord


def upsert_chat(conn: sqlite3.Connection, chat: ChatRecord) -> int:
    """Insert a chat or refresh its last-seen metadata. Returns the internal id."""
    now = _now()
    conn.execute(
        """
        INSERT INTO chats (source, source_chat_id, display_name, chat_type, status,
                           first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, 'discovered', ?, ?)
        ON CONFLICT(source, source_chat_id) DO UPDATE SET
            display_name = excluded.display_name,
            chat_type    = excluded.chat_type,
            last_seen_at = excluded.last_seen_at
        """,
        (chat.source, chat.source_chat_id, chat.display_name, chat.chat_type, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM chats WHERE source = ? AND source_chat_id = ?",
        (chat.source, chat.source_chat_id),
    ).fetchone()
    return int(row["id"])


def set_chat_status(conn: sqlite3.Connection, chat_id: int, status: str) -> bool:
    """Set a chat's status ('monitored'|'ignored'|'discovered'). True if a row changed."""
    cur = conn.execute("UPDATE chats SET status = ? WHERE id = ?", (status, chat_id))
    conn.commit()
    return cur.rowcount > 0


def set_chat_alias(conn: sqlite3.Connection, chat_id: int, alias: str | None) -> bool:
    """Set (or clear, with None) a chat's operator alias. True if a row changed.

    The alias overrides the connector-derived ``display_name`` in the UI; an empty
    or whitespace-only value is normalized to NULL so it falls back to that name.
    """
    cleaned = alias.strip() if alias else None
    cur = conn.execute(
        "UPDATE chats SET alias = ? WHERE id = ?", (cleaned or None, chat_id)
    )
    conn.commit()
    return cur.rowcount > 0


class LinkError(ValueError):
    """An operator link request that violates the parent↔child rules.

    Raised by :func:`link_chats` so the API can translate it to a 400. Existence
    (404) is checked by the caller before linking.
    """


def child_count(conn: sqlite3.Connection, parent_id: int) -> int:
    """How many chats are linked as children of ``parent_id`` (0 if it's not a parent)."""
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM chats WHERE parent_chat_id = ?", (parent_id,)
        ).fetchone()["n"]
    )


def child_chats(conn: sqlite3.Connection, parent_id: int) -> list[sqlite3.Row]:
    """The child chats linked under ``parent_id``, ordered by id (empty if none)."""
    return list(
        conn.execute(
            "SELECT id, source_chat_id, display_name, alias, chat_type, status, "
            "last_message_at, parent_chat_id FROM chats WHERE parent_chat_id = ? ORDER BY id",
            (parent_id,),
        ).fetchall()
    )


def family_member_ids(conn: sqlite3.Connection, head_id: int) -> list[int]:
    """Chat ids that make up a family: the head first, then its children by id.

    For a standalone or childless chat this is just ``[head_id]``. The review path
    folds these members' deltas into one analysis under the head; each member still
    keeps its own per-chat cursor.
    """
    rows = conn.execute(
        "SELECT id FROM chats WHERE parent_chat_id = ? ORDER BY id", (head_id,)
    ).fetchall()
    return [head_id, *(int(r["id"]) for r in rows)]


def link_chats(conn: sqlite3.Connection, child_id: int, parent_id: int) -> None:
    """Link ``child_id`` as a child of ``parent_id`` (also used to re-parent).

    The link lives on the child (``parent_chat_id``). Enforces depth-1 families:
    a chat can't link to itself, can't link under a chat that is itself a child,
    and a chat that already has children can't become a child. Raises
    :class:`LinkError` on any violation. Callers verify both chats exist first.
    """
    if child_id == parent_id:
        raise LinkError("a chat cannot be linked to itself")
    parent = get_chat(conn, parent_id)
    if parent is None:
        raise LinkError("parent chat not found")
    if parent["parent_chat_id"] is not None:
        raise LinkError("cannot link under a chat that is itself a child")
    if child_count(conn, child_id) > 0:
        raise LinkError("cannot link a chat that already has linked children")
    conn.execute(
        "UPDATE chats SET parent_chat_id = ? WHERE id = ?", (parent_id, child_id)
    )
    conn.commit()


def unlink_chat(conn: sqlite3.Connection, child_id: int) -> bool:
    """Clear a chat's parent link, restoring it as an independent chat.

    Returns True if the chat was a child (a row changed); False if it had no link.
    No message data or cursor is touched — only the link metadata is removed.
    """
    cur = conn.execute(
        "UPDATE chats SET parent_chat_id = NULL WHERE id = ? AND parent_chat_id IS NOT NULL",
        (child_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def list_chats(
    conn: sqlite3.Connection, *, order_by_recent: bool = False
) -> list[sqlite3.Row]:
    # ``order_by_recent`` lists the most recently-active chats first (NULLs last),
    # which is how an operator scans a large account to pick what to monitor.
    order = (
        "ORDER BY last_message_at IS NULL, last_message_at DESC, id"
        if order_by_recent
        else "ORDER BY id"
    )
    return list(
        conn.execute(
            "SELECT id, source, source_chat_id, display_name, alias, chat_type, status, "
            f"last_message_at FROM chats {order}"
        ).fetchall()
    )


def monitored_chats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # Review iterates *family heads* only: a monitored chat that has been linked
    # as a child (``parent_chat_id`` set) is folded into its parent's family
    # review, never reviewed standalone. Its own status is left intact and takes
    # effect again once it is unlinked.
    return list(
        conn.execute(
            "SELECT id, source_chat_id, display_name FROM chats "
            "WHERE status = 'monitored' AND parent_chat_id IS NULL ORDER BY id"
        ).fetchall()
    )


def chat_id_for_source(
    conn: sqlite3.Connection, source_chat_id: str, *, source: str = "whatsapp"
) -> int | None:
    """Return the internal id for a chat's ``(source, source_chat_id)``, or None.

    Chat identity is the composite ``(source, source_chat_id)`` (#57); ``source``
    defaults to ``'whatsapp'`` so existing single-source callers are unchanged.
    The resync path uses this to classify an incoming chat as new (insert) vs
    existing (compare-then-maybe-update) without an upsert that would always
    touch ``last_seen_at`` and so report a phantom change on a no-op run.
    """
    row = conn.execute(
        "SELECT id FROM chats WHERE source = ? AND source_chat_id = ?",
        (source, source_chat_id),
    ).fetchone()
    return int(row["id"]) if row else None


def get_chat(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    """Return a single chat row by internal id, or None if it doesn't exist."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, source, source_chat_id, display_name, alias, chat_type, status, "
        "last_message_at, parent_chat_id FROM chats WHERE id = ?",
        (chat_id,),
    ).fetchone()
    return row


def count_chats(conn: sqlite3.Connection) -> int:
    """Total chats stored, regardless of status."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"])
