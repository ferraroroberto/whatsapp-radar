"""Send an actionable verdict's messages to task-os's Inbox without a tap.

A scan calls :func:`export_actionable` once per actionable verdict when
``task_os.auto_export`` is on. It reuses the per-message export the Chats
overlay's Send to Task-OS button drives, and the same idempotency: a message
whose ``messages.task_exported_at`` is set is never posted again, so a manual
tap afterwards (or a re-run over the same delta) creates no duplicate.

Best-effort by design: a failed export is logged and left unexported for the
next tap, and never holds the chat's cursor or the digest. Analysis state is
already persisted when this runs.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any

from src.config import TaskOsConfig
from src.db import store
from src.models import StoredMessage, TaskExportContext
from src.task_os import client

logger = logging.getLogger(__name__)

Exporter = Callable[[TaskOsConfig, TaskExportContext], Any]


def pick_messages(
    delta: Sequence[StoredMessage], evidence_message_ids: Sequence[str]
) -> list[StoredMessage]:
    """The delta messages to export for one actionable verdict.

    The classifier's cited evidence, in delta order, when it named any message
    in this delta; otherwise the newest message with text, so an actionable
    verdict without usable evidence still lands as one task rather than none.
    """
    cited = set(evidence_message_ids)
    picked = [m for m in delta if m.source_message_id in cited and (m.text or "").strip()]
    if picked:
        return picked
    with_text = [m for m in delta if (m.text or "").strip()]
    return with_text[-1:]


def export_actionable(
    conn: sqlite3.Connection,
    config: TaskOsConfig,
    delta: Sequence[StoredMessage],
    evidence_message_ids: Sequence[str],
    *,
    exporter: Exporter | None = None,
) -> int:
    """Export one verdict's messages; return how many were newly sent.

    A no-op returning 0 unless ``auto_export`` is on and export is ready
    (enabled with a token). ``exporter`` is injectable for tests; production
    posts through :func:`src.task_os.client.export_message`.
    """
    if not (config.auto_export and config.ready):
        return 0
    send = exporter or client.export_message
    sent = 0
    for message in pick_messages(delta, evidence_message_ids):
        ctx = store.message_task_export_context(conn, message.id)
        if ctx is None or ctx.task_exported_at is not None:
            continue
        try:
            send(config, ctx)
        except client.TaskOsError as exc:
            logger.warning(
                "⚠️ task-os auto-export failed for message %s (status %s): %s",
                message.id, exc.status, exc,
            )
            continue
        store.set_task_exported(conn, message.id)
        sent += 1
    if sent:
        logger.info("ℹ️ task-os auto-export sent %d message(s)", sent)
    return sent
