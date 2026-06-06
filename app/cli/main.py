"""Command-line entry point.

Commands: status | ingest | chats | monitor | ignore | review | scan | notify.
The CLI wires the boundaries together but holds no business logic of its own.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from src.analysis.classifier import build_classifier
from src.analysis.pipeline import Mode, scan
from src.analysis.review import review_monitored_chats
from src.config import Config, load_config
from src.connector.base import MessageConnector
from src.connector.factory import build_connector
from src.db import store
from src.notify import deliver_digest
from src.report.digest import Digest, build_digest


def _build_connector(config: Config) -> MessageConnector:
    try:
        return build_connector(config)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


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
        new_messages += store.insert_messages(conn, chat_id, connector.fetch_messages(
            chat.source_chat_id
        ))
    connector.stop()
    print(f"Ingested {new_chats} chats, {new_messages} new messages.")
    return 0


def _cmd_chats(conn: sqlite3.Connection, recent: bool, limit: int | None) -> int:
    chats = store.list_chats(conn, order_by_recent=recent)
    if not chats:
        print("No chats yet. Run 'wr ingest' first.")
        return 0
    shown = chats[:limit] if limit else chats
    for c in shown:
        last = (c["last_message_at"] or "")[:16].replace("T", " ")
        print(f"[{c['id']:>4}] {c['status']:<10} {last:<16}  {c['display_name']}")
    if limit and len(chats) > limit:
        print(f"… {len(chats) - limit} more (showing {limit} of {len(chats)}).")
    return 0


def _cmd_set_status(conn: sqlite3.Connection, chat_id: int, status: str) -> int:
    if store.set_chat_status(conn, chat_id, status):
        msg = f"Chat {chat_id} set to {status}."
        if status == "monitored" and store.baseline_cursor(conn, chat_id):
            msg += " Cursor baselined to latest — only new messages will be reviewed."
        print(msg)
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
        return 0
    return _deliver(conn, config, outcome.run_id, digest)


def _cmd_scan(
    conn: sqlite3.Connection, config: Config, dry_run: bool, days: int | None
) -> int:
    """Unified pipeline: sync -> Stage 1 -> Stage 2 -> digest -> deliver, with a trace."""
    mode: Mode = "dry_run" if dry_run else "live"
    connector = None if dry_run else _build_connector(config)
    outcome = scan(conn, config, mode=mode, days=days, connector=connector)

    if outcome.digest is not None:
        print(outcome.digest.to_json())
    print(
        f"\nRun {outcome.run_id} [{mode}]: synced {outcome.chats_synced} chats / "
        f"{outcome.messages_synced} msgs, {outcome.chats_monitored} monitored, "
        f"{outcome.chats_with_delta} with delta, stage1={outcome.stage1_passed}, "
        f"llm={outcome.stage2_llm_calls}, actionable={outcome.actionable}, "
        f"notify={outcome.notification_status}.",
        file=sys.stderr,
    )
    for chat_id, err in outcome.errors:
        print(f"  ! chat {chat_id} skipped (cursor not advanced): {err}", file=sys.stderr)
    return 1 if outcome.notification_status == "failed" else 0


def _deliver(conn: sqlite3.Connection, config: Config, run_id: int, digest: Digest) -> int:
    """Deliver a run's digest, recording the outcome. Retryable via 'wr notify'."""
    status, detail = deliver_digest(conn, config, run_id, digest)
    if status == "failed":
        print(f"Delivery failed (retry with 'wr notify'): {detail}", file=sys.stderr)
        return 1
    if status == "skipped":
        print("Notifier is 'none' — digest recorded as skipped (set WR_NOTIFIER=telegram).",
              file=sys.stderr)
        return 0
    print(f"Digest delivered via {config.notifier}.", file=sys.stderr)
    return 0


def _cmd_notify(conn: sqlite3.Connection, config: Config, run_id: int | None) -> int:
    """(Re)deliver the digest for a run — the latest run by default."""
    rid = run_id if run_id is not None else store.latest_run_id(conn)
    if rid is None:
        print("No review run to deliver. Run 'wr review' first.", file=sys.stderr)
        return 1
    digest = build_digest(conn, rid)
    if not digest.has_actionable_items:
        print(f"Run {rid} has no actionable items — nothing to deliver.", file=sys.stderr)
        return 0
    return _deliver(conn, config, rid, digest)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wr", description="WhatsApp Radar (read-only spike).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show connector and DB status")
    sub.add_parser("ingest", help="ingest chats and messages from the connector")
    p_chats = sub.add_parser("chats", help="list discovered chats")
    p_chats.add_argument(
        "--recent", action="store_true", help="order by most recent message first"
    )
    p_chats.add_argument("--limit", type=int, default=None, help="show only the first N chats")

    p_mon = sub.add_parser("monitor", help="mark a chat as monitored")
    p_mon.add_argument("chat_id", type=int)
    p_ign = sub.add_parser("ignore", help="mark a chat as ignored")
    p_ign.add_argument("chat_id", type=int)

    p_rev = sub.add_parser("review", help="review monitored chats since the last cursor")
    p_rev.add_argument(
        "--dry-run", action="store_true", help="print the digest without delivering it"
    )

    p_scan = sub.add_parser(
        "scan", help="unified sync -> analyze -> digest -> notify, with a full audit trace"
    )
    p_scan.add_argument(
        "--dry-run",
        action="store_true",
        help="replay stored messages with no connector, no delivery, no cursor advance",
    )
    p_scan.add_argument(
        "--days", type=int, default=None, help="dry-run: only replay messages from the last N days"
    )

    p_notify = sub.add_parser("notify", help="(re)deliver a run's digest (latest by default)")
    p_notify.add_argument(
        "--run", type=int, default=None, help="run id to deliver (default: latest)"
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
            return _cmd_chats(conn, args.recent, args.limit)
        if args.command == "monitor":
            return _cmd_set_status(conn, args.chat_id, "monitored")
        if args.command == "ignore":
            return _cmd_set_status(conn, args.chat_id, "ignored")
        if args.command == "review":
            return _cmd_review(conn, config, args.dry_run)
        if args.command == "scan":
            return _cmd_scan(conn, config, args.dry_run, args.days)
        if args.command == "notify":
            return _cmd_notify(conn, config, args.run)
    finally:
        conn.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
