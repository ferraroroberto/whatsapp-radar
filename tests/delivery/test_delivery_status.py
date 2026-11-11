"""The delivery status vocabulary is declared once and refused outside it."""

from __future__ import annotations

import sqlite3

import pytest

from src.db import store
from src.delivery.status import (
    DELIVERY_CLASSIFIER_OFFLINE,
    DELIVERY_DRY_RUN,
    DELIVERY_FAILED,
    DELIVERY_NONE,
    DELIVERY_OFFLINE,
    DELIVERY_SENT,
    DELIVERY_SKIPPED,
    DELIVERY_STATUSES,
    SEND_STATUSES,
    require_send_status,
    run_delivery_ok,
)


@pytest.mark.parametrize(
    ("status", "ok"),
    [
        (DELIVERY_SENT, True),
        (DELIVERY_SKIPPED, True),
        (DELIVERY_NONE, True),
        (DELIVERY_DRY_RUN, True),
        (DELIVERY_FAILED, False),
        (DELIVERY_OFFLINE, False),
        (DELIVERY_CLASSIFIER_OFFLINE, False),
    ],
)
def test_every_declared_status_has_one_verdict(status: str, ok: bool) -> None:
    assert status in DELIVERY_STATUSES
    assert run_delivery_ok(status) is ok


def test_a_single_send_can_only_end_in_a_send_status() -> None:
    assert SEND_STATUSES == {DELIVERY_SENT, DELIVERY_SKIPPED, DELIVERY_FAILED}
    assert SEND_STATUSES < DELIVERY_STATUSES


# state-machine-proof: delivery-status
def test_the_store_refuses_a_notification_status_outside_a_send(
    conn: sqlite3.Connection,
) -> None:
    run_id = store.start_run(conn, mode="live")
    for status in (DELIVERY_OFFLINE, DELIVERY_NONE, "bogus"):
        with pytest.raises(ValueError):
            store.record_notification(conn, run_id, "telegram", status)
    with pytest.raises(ValueError):
        require_send_status("bogus")
    count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert count == 0


# state-machine-proof: delivery-status
def test_a_run_funnel_refuses_an_unknown_delivery_status() -> None:
    with pytest.raises(ValueError):
        store.RunFunnel(
            chats_synced=0, messages_synced=0, chats_monitored=0, stage1_passed=0,
            stage2_llm_calls=0, actionable=0, notification_status="bogus",
        )
