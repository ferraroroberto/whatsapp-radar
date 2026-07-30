"""Reusable digest delivery, recording the notifications row.

Kept out of the CLI so ``wr review`` / ``wr notify`` and the unified scan
pipeline all deliver through one code path. Delivery stays decoupled from
analysis: a failure here never touches analysis state or cursors, so the same
run can be re-delivered later (``wr notify``) without re-analysing anything.
"""

from __future__ import annotations

import sqlite3

from src.config import Config
from src.db import store
from src.notify.factory import _dispatch
from src.report.digest import Digest


def deliver_digest(
    conn: sqlite3.Connection, config: Config, run_id: int, digest: Digest
) -> tuple[str, str | None]:
    """Deliver a run's digest, recording the outcome. Returns ``(status, detail)``.

    ``status`` is one of ``'sent'`` | ``'skipped'`` (notifier is 'none') |
    ``'failed'`` (misconfigured or send error). ``detail`` carries the error
    message on failure/skip, else ``None``.
    """
    status, detail = _dispatch(config, lambda notifier: notifier.send(digest))
    store.record_notification(conn, run_id, config.notifier, status, detail)
    return status, detail
