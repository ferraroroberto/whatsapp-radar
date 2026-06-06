"""Command-line entry point.

Commands: status | ingest | chats | monitor | ignore | review. The CLI wires the
boundaries together but holds no business logic of its own.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from .analysis.classifier import build_classifier
from .analysis.review import review_monitored_chats
from .config import Config, load_config
from .connector.base import MessageConnector
from .connector.fixture import FixtureConnector
from .db import store
from .report.digest import build_digest


def _build_connector(config: Config) -> MessageConnector:
    if config.connector == "fixture":
        return FixtureConnector()
    raise SystemExit(
        f"connector {config.connector!r} is not available in this spike "
        "(only 'fixture'; linked-device is deferred)"
    )


def _cmd_status(conn: sqlite3.Connection, config: Config) -> int:
    connector = _build_connector(config)
    cstatus = connector.connect()
    chats = store.list_chats(conn)
    monitored = sum(1 for c in chats if c["status"] == "monitored")
    print(f"DB:         {config.db_path}")
    print(f"Connector:  {cstatus.name} (connected={cstatus.connected}) — {cstatus.detail}")
    print(f"Classifier: {config.classifier}")
    print(f"Chats:      {len(chats)} discovered, {monitored} monitored")
    return 0


def _cmd_ingest(conn: sqlite3.Connection, config: Config) -> int:
    connector = _build_connector(config)
    connector.connect()
    new_chats = 0
    new_messages = 0
    for chat in connector.list_chats():
        chat_id = store.upsert_chat(conn, chat)
        new_chats += 1
        for msg in connector.fetch_messages(chat.source_chat_id):
            if store.insert_message(conn, chat_id, msg):
                new_messages += 1
    connector.stop()
    print(f"Ingested {new_chats} chats, {new_messages} new messages.")
    return 0


def _cmd_chats(conn: sqlite3.Connection, config: Config) -> int:
    chats = store.list_chats(conn)
    if not chats:
        print("No chats yet. Run 'wr ingest' first.")
        return 0
    for c in chats:
        print(f"[{c['id']:>3}] {c['status']:<10} {c['display_name']}")
    return 0


def _cmd_set_status(conn: sqlite3.Connection, chat_id: int, status: str) -> int:
    if store.set_chat_status(conn, chat_id, status):
        print(f"Chat {chat_id} set to {status}.")
        return 0
    print(f"No chat with id {chat_id}.")
    return 1


def _cmd_review(conn: sqlite3.Connection, config: Config, dry_run: bool) -> int:
    classifier = build_classifier(config.classifier, config.hub)
    outcome = review_monitored_chats(conn, classifier)
    digest = build_digest(conn, outcome.run_id)

    print(digest.to_json())
    print(
        f"\nRun {outcome.run_id}: {outcome.chats_with_delta} chats with new messages, "
        f"{outcome.messages_processed} messages processed, "
        f"{outcome.actionable_chats} actionable.",
        file=sys.stderr,
    )
    for chat_id, err in outcome.errors:
        print(f"  ! chat {chat_id} skipped (cursor not advanced): {err}", file=sys.stderr)

    if not digest.has_actionable_items:
        print("No actionable items — no notification.", file=sys.stderr)
        return 0

    if dry_run:
        print("Dry run — digest not delivered.", file=sys.stderr)
    else:
        # Telegram delivery is deferred (onboarding step 8). Record the intent so the
        # follow-up issue can wire a retryable notifier without changing this flow.
        store.record_notification(
            conn, outcome.run_id, "telegram", "skipped", "no notifier configured (deferred)"
        )
        print("Notifier not configured yet (deferred) — recorded as skipped.", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wr", description="WhatsApp Radar (read-only spike).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show connector and DB status")
    sub.add_parser("ingest", help="ingest chats and messages from the connector")
    sub.add_parser("chats", help="list discovered chats")

    p_mon = sub.add_parser("monitor", help="mark a chat as monitored")
    p_mon.add_argument("chat_id", type=int)
    p_ign = sub.add_parser("ignore", help="mark a chat as ignored")
    p_ign.add_argument("chat_id", type=int)

    p_rev = sub.add_parser("review", help="review monitored chats since the last cursor")
    p_rev.add_argument(
        "--dry-run", action="store_true", help="print the digest without delivering it"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)
    config = load_config()
    conn = store.connect(config.db_path)
    try:
        if args.command == "status":
            return _cmd_status(conn, config)
        if args.command == "ingest":
            return _cmd_ingest(conn, config)
        if args.command == "chats":
            return _cmd_chats(conn, config)
        if args.command == "monitor":
            return _cmd_set_status(conn, args.chat_id, "monitored")
        if args.command == "ignore":
            return _cmd_set_status(conn, args.chat_id, "ignored")
        if args.command == "review":
            return _cmd_review(conn, config, args.dry_run)
    finally:
        conn.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
