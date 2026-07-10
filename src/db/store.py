"""SQLite store: connection/migration plus typed repository functions.

Storage owns chat metadata, messages, the per-chat review cursor, review runs,
analysis results, and notification state. Cursor advancement is exposed as an
explicit call (:func:`advance_cursor`) so callers can guarantee it happens only
after analysis has been persisted.

This module is a thin re-export facade. The implementation is split across
cohesive submodules under ``src/db/`` — ``connection`` (open/migrate + shared
helpers), ``chats``, ``messages`` (incl. voice-note transcription),
``runs`` (review runs/analysis/notifications), ``dashboard`` (cross-table
read-only aggregates), ``sync_log``, and ``reprocess_support`` — so that
``from src.db import store`` / ``store.<name>`` call sites across the repo
keep working unchanged. See each submodule's docstring for what it owns.
"""

from __future__ import annotations

from src.db.chats import (
    LinkError,
    chat_id_for_source,
    child_chats,
    child_count,
    count_chats,
    family_member_ids,
    get_chat,
    link_chats,
    list_chats,
    monitored_chats,
    set_chat_alias,
    set_chat_status,
    unlink_chat,
    upsert_chat,
)
from src.db.connection import connect
from src.db.dashboard import chats_overview, count_chats_by_status, messages_per_chat
from src.db.messages import (
    advance_cursor,
    baseline_cursor,
    clear_media_path,
    count_messages_since,
    expired_retained_audio,
    family_delta_replay,
    family_delta_since_cursor,
    insert_message,
    insert_messages,
    mark_transcription,
    message_count_total,
    message_text,
    messages_for_chat,
    messages_since_cursor,
    pending_transcriptions,
    recent_actionable_items,
    recent_messages,
    recent_messages_family,
    stale_voice_notes,
    voice_audio_path,
)
from src.db.reprocess_support import clear_all_data, snapshot_operator_state
from src.db.runs import (
    actionable_items_for_run,
    count_actionable_items,
    count_notifications_sent,
    count_runs,
    finish_run,
    insert_analysis_item,
    insert_analysis_trace,
    last_run,
    latest_run_id,
    list_review_runs,
    record_notification,
    record_run_funnel,
    review_run,
    start_run,
    traces_for_run,
)
from src.db.sync_log import recent_syncs, record_sync

__all__ = [
    "LinkError",
    "actionable_items_for_run",
    "advance_cursor",
    "baseline_cursor",
    "chat_id_for_source",
    "chats_overview",
    "child_chats",
    "child_count",
    "clear_all_data",
    "clear_media_path",
    "connect",
    "count_actionable_items",
    "count_chats",
    "count_chats_by_status",
    "count_messages_since",
    "count_notifications_sent",
    "count_runs",
    "expired_retained_audio",
    "family_delta_replay",
    "family_delta_since_cursor",
    "family_member_ids",
    "finish_run",
    "get_chat",
    "insert_analysis_item",
    "insert_analysis_trace",
    "insert_message",
    "insert_messages",
    "last_run",
    "latest_run_id",
    "link_chats",
    "list_chats",
    "list_review_runs",
    "mark_transcription",
    "message_count_total",
    "message_text",
    "messages_for_chat",
    "messages_per_chat",
    "messages_since_cursor",
    "monitored_chats",
    "pending_transcriptions",
    "recent_actionable_items",
    "recent_messages",
    "recent_messages_family",
    "recent_syncs",
    "record_notification",
    "record_run_funnel",
    "record_sync",
    "review_run",
    "set_chat_alias",
    "set_chat_status",
    "snapshot_operator_state",
    "stale_voice_notes",
    "start_run",
    "traces_for_run",
    "unlink_chat",
    "upsert_chat",
    "voice_audio_path",
]
