"""Dashboard aggregates (store) + the /api/dashboard endpoint.

The store functions are asserted against a hand-seeded fixture DB so the numbers
are known exactly; the endpoint test checks the JSON shape + the bearer gate.
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


def _seed(conn: sqlite3.Connection) -> None:
    """Two monitored chats (3 + 2 messages), one ignored chat, one run with
    one actionable verdict and one delivered notification."""
    a = store.upsert_chat(
        conn, ChatRecord(source_chat_id="g1", display_name="Class 4A Group", chat_type="group")
    )
    b = store.upsert_chat(
        conn,
        ChatRecord(source_chat_id="g2", display_name="School Parents Group", chat_type="group"),
    )
    c = store.upsert_chat(
        conn, ChatRecord(source_chat_id="g3", display_name="Random Group", chat_type="group")
    )
    store.set_chat_status(conn, a, "monitored")
    store.set_chat_status(conn, b, "monitored")
    store.set_chat_status(conn, c, "ignored")

    for i in range(3):
        store.insert_message(
            conn,
            a,
            MessageRecord(
                source_message_id=f"a{i}",
                message_timestamp=f"2026-06-01T10:0{i}:00+00:00",
                text="hi",
                sender_label="X",
            ),
        )
    for i in range(2):
        store.insert_message(
            conn,
            b,
            MessageRecord(
                source_message_id=f"b{i}",
                message_timestamp=f"2026-06-01T11:0{i}:00+00:00",
                text="yo",
                sender_label="Y",
            ),
        )

    run_id = store.start_run(conn, mode="live")
    store.record_run_funnel(
        conn,
        run_id,
        chats_synced=3,
        messages_synced=5,
        chats_monitored=2,
        stage1_passed=1,
        stage2_llm_calls=1,
        actionable=1,
        notification_status="sent",
    )
    store.finish_run(conn, run_id, "completed", chats_reviewed=2)
    store.insert_analysis_item(
        conn,
        run_id,
        a,
        action_required=True,
        priority="high",
        summary="pick-up change",
        suggested_next_action=None,
        deadline=None,
        confidence=0.9,
        evidence_message_ids_json=None,
    )
    store.insert_analysis_item(
        conn,
        run_id,
        b,
        action_required=False,
        priority=None,
        summary=None,
        suggested_next_action=None,
        deadline=None,
        confidence=None,
        evidence_message_ids_json=None,
    )
    store.record_notification(conn, run_id, "telegram", "sent")


# --- store aggregates -------------------------------------------------------

def test_aggregates_reconcile(conn: sqlite3.Connection) -> None:
    _seed(conn)

    assert store.count_chats_by_status(conn) == {
        "discovered": 0,
        "monitored": 2,
        "ignored": 1,
    }
    assert store.message_count_total(conn) == 5

    per_chat = store.messages_per_chat(conn, monitored_only=True)
    # Ordered by last message descending: School Parents (11:01) before Class 4A (10:02).
    assert [(r["display_name"], r["message_count"]) for r in per_chat] == [
        ("School Parents Group", 2),
        ("Class 4A Group", 3),
    ]
    # The ignored chat is excluded from the monitored-only view.
    assert len(store.messages_per_chat(conn, monitored_only=False)) == 3

    assert store.count_runs(conn) == 1
    last = store.last_run(conn)
    assert last is not None and last["mode"] == "live"

    # Deterministic backlog bounds: everything is after epoch, nothing after 2999.
    assert store.count_messages_since(conn, "2000-01-01T00:00:00+00:00") == 5
    assert store.count_messages_since(conn, "2999-01-01T00:00:00+00:00") == 0

    assert store.count_actionable_items(conn) == 1
    assert store.count_notifications_sent(conn) == 1


def test_messages_per_chat_folds_linked_family(conn: sqlite3.Connection) -> None:
    """A monitored parent's row aggregates its linked children: summed count and
    the family's max last-message time, so the Dashboard matches the Chats tab
    (#42). A child's newer message floats the parent to the top."""
    parent = store.upsert_chat(
        conn, ChatRecord(source_chat_id="p", display_name="School Office", chat_type="dm")
    )
    child = store.upsert_chat(
        conn, ChatRecord(source_chat_id="c", display_name="+44987", chat_type="dm")
    )
    other = store.upsert_chat(
        conn, ChatRecord(source_chat_id="o", display_name="Class 4A Group", chat_type="group")
    )
    store.set_chat_status(conn, parent, "monitored")
    store.set_chat_status(conn, other, "monitored")
    store.insert_message(
        conn, parent,
        MessageRecord(source_message_id="p1", message_timestamp="2026-06-01T10:00:00+00:00",
                      text="old", sender_label="O"),
    )
    store.insert_message(
        conn, child,
        MessageRecord(source_message_id="c1", message_timestamp="2026-06-07T12:00:00+00:00",
                      text="new", sender_label="O"),
    )
    store.insert_message(
        conn, other,
        MessageRecord(source_message_id="o1", message_timestamp="2026-06-05T09:00:00+00:00",
                      text="hi", sender_label="X"),
    )
    store.link_chats(conn, child, parent)

    rows = store.messages_per_chat(conn, monitored_only=True)
    # The child is folded into the parent (not its own row); two monitored heads.
    by_id = {int(r["id"]): r for r in rows}
    assert set(by_id) == {parent, other}
    # Parent shows the family's summed count and the child's newer last-message.
    assert by_id[parent]["message_count"] == 2
    assert by_id[parent]["last_message_at"] == "2026-06-07T12:00:00+00:00"
    # And it sorts first, ahead of the other monitored chat (05 Jun).
    assert [int(r["id"]) for r in rows][0] == parent


def test_aggregates_empty_db(conn: sqlite3.Connection) -> None:
    assert store.count_chats_by_status(conn) == {
        "discovered": 0,
        "monitored": 0,
        "ignored": 0,
    }
    assert store.message_count_total(conn) == 0
    assert store.count_runs(conn) == 0
    assert store.last_run(conn) is None
    assert store.count_actionable_items(conn) == 0


# --- endpoint ---------------------------------------------------------------

def test_dashboard_endpoint_numbers(tmp_path: Path) -> None:
    db = tmp_path / "dash.sqlite3"
    conn = store.connect(db)
    _seed(conn)
    conn.close()

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    with TestClient(app, client=LOOPBACK) as client:
        body = client.get("/api/dashboard").json()

    assert body["chats"] == {"discovered": 0, "monitored": 2, "ignored": 1, "total": 3}
    assert body["messages"]["total"] == 5
    assert len(body["messages"]["per_channel"]) == 2
    # Most recently active channel first.
    assert body["messages"]["per_channel"][0]["name"] == "School Parents Group"
    assert body["scans"]["count"] == 1
    assert body["scans"]["last"]["mode"] == "live"
    assert body["alerts"] == {"actionable": 1, "notifications_sent": 1}
    # One last-activity card per kind, in a fixed order (#165).
    assert [c["source"] for c in body["last_activity"]] == [
        "whatsapp", "gmail", "traffic", "calendar"
    ]


# --- last-activity cards (#165) ---------------------------------------------

def test_last_activity_cards_distill_each_kind(tmp_path: Path) -> None:
    db = tmp_path / "act.sqlite3"
    conn = store.connect(db)
    # A message-pipeline scan carrying a per-source funnel for WhatsApp + Gmail.
    rid = store.start_run(conn, mode="live", kind="scan")
    store.record_run_funnel(
        conn,
        rid,
        chats_synced=2,
        messages_synced=12,
        chats_monitored=3,
        stage1_passed=2,
        stage2_llm_calls=1,
        actionable=1,
        notification_status="sent",
        source_funnel_json=json.dumps(
            {
                "whatsapp": {"messages_synced": 12, "actionable": 1},
                "gmail": {"messages_synced": 4, "actionable": 0},
            }
        ),
    )
    store.finish_run(conn, rid, "completed", chats_reviewed=3)
    # A traffic check that raised one delay alert.
    tid = store.start_run(conn, mode="live", kind="traffic-check")
    store.finish_run_summary(
        conn, tid, "completed",
        json.dumps({"kind": "traffic-check", "status": "ok",
                    "checked": [{}, {}], "alerts": 1}),
    )
    # A calendar scan with nothing wrong.
    cid = store.start_run(conn, mode="live", kind="calendar-scan")
    store.finish_run_summary(
        conn, cid, "completed",
        json.dumps({"kind": "calendar-scan", "status": "ok",
                    "conflicts": [], "unknown_locations": []}),
    )
    conn.close()

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    with TestClient(app, client=LOOPBACK) as client:
        cards = {c["source"]: c for c in client.get("/api/dashboard").json()["last_activity"]}

    assert cards["whatsapp"]["summary"] == "12 new · 1 actionable"
    assert cards["whatsapp"]["alerts"] == 1
    assert cards["whatsapp"]["kind"] == "scan"
    assert cards["whatsapp"]["db_run_id"] == rid
    assert cards["gmail"]["summary"] == "4 new · 0 actionable"
    assert cards["gmail"]["alerts"] == 0
    assert cards["traffic"]["summary"] == "1 delay alert"
    assert cards["traffic"]["alerts"] == 1
    assert cards["calendar"]["summary"] == "no conflicts"
    assert cards["calendar"]["alerts"] == 0


def test_last_activity_never_ran_on_empty_db(tmp_path: Path) -> None:
    db = tmp_path / "act-empty.sqlite3"
    store.connect(db).close()

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    with TestClient(app, client=LOOPBACK) as client:
        cards = client.get("/api/dashboard").json()["last_activity"]

    assert [c["source"] for c in cards] == ["whatsapp", "gmail", "traffic", "calendar"]
    for card in cards:
        assert card["kind"] is None
        assert card["started_at"] is None
        assert card["summary"] == ""
        assert card["alerts"] == 0


def test_dashboard_empty_db_all_zeros(tmp_path: Path) -> None:
    db = tmp_path / "empty.sqlite3"
    store.connect(db).close()

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    with TestClient(app, client=LOOPBACK) as client:
        body = client.get("/api/dashboard").json()

    assert body["chats"]["total"] == 0
    assert body["messages"]["per_channel"] == []
    assert body["scans"]["last"] is None
    assert body["scans"]["messages_since_last"] == 0


def test_dashboard_requires_token_from_remote(tmp_path: Path) -> None:
    db = tmp_path / "gated.sqlite3"
    store.connect(db).close()

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="secret")
    app.state.db_path = db
    with TestClient(app, client=REMOTE) as client:
        assert client.get("/api/dashboard").status_code == 401
        ok = client.get("/api/dashboard", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
