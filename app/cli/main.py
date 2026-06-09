"""Command-line entry point.

Commands: status | ingest | chats | monitor | ignore | review | scan | notify |
resync | reprocess. The CLI wires the boundaries together but holds no business
logic of its own.

``scan``, ``resync`` and ``reprocess`` are the launchable Execution-tab pieces:
each streams human-readable progress to stdout and prints one final structured
``__WR_RESULT__`` sentinel line the webapp parses for the funnel/counts. They run
identically whether fired here, from App Launcher Jobs, or spawned by the webapp.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from src.analysis.classifier import build_classifier
from src.analysis.pipeline import Mode, scan, scan_outcome_to_dict
from src.analysis.review import review_monitored_chats
from src.config import Config, load_config
from src.connector.base import MessageConnector
from src.connector.factory import build_connector
from src.connector.preflight import ConnectorOffline, preflight
from src.db import store
from src.db.reprocess import reprocess, reprocess_outcome_to_dict
from src.db.sync import resync, resync_outcome_to_dict
from src.notify import deliver_digest
from src.notify.alert import send_alert
from src.report.digest import Digest, build_digest
from src.runresult import format_result


def _progress(line: str) -> None:
    """Stream one progress line to stdout (captured into the run's output.log)."""
    print(line, flush=True)


def _emit_result(payload: dict[str, object]) -> None:
    """Print the final structured result sentinel the webapp parses."""
    print(format_result(payload), flush=True)


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
        alias = c["alias"]
        label = f"{alias} ({c['display_name']})" if alias else c["display_name"]
        print(f"[{c['id']:>4}] {c['status']:<10} {last:<16}  {label}")
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
    """Process piece: analyze monitored deltas since each cursor (no sync)."""
    classifier = build_classifier(config.classifier, config.hub)
    _progress("▶ process starting — analyzing monitored chats since last cursor")
    outcome = review_monitored_chats(
        conn, classifier, since_days=config.hub.recent_alert_days
    )
    digest = build_digest(conn, outcome.run_id)

    for chat_id, err in outcome.errors:
        print(f"  ! chat {chat_id} skipped (cursor not advanced): {err}", file=sys.stderr)

    if not digest.has_actionable_items:
        notif, rc = "none", 0
    elif dry_run:
        notif, rc = "dry_run", 0
    else:
        notif, rc = _deliver(conn, config, outcome.run_id, digest)

    _progress(
        f"✓ process done — {outcome.chats_with_delta} chat(s) with delta, "
        f"{outcome.messages_processed} msg(s) processed, "
        f"{outcome.actionable_chats} actionable, notify {notif}"
    )
    _emit_result(
        {
            "kind": "process",
            "ok": notif != "failed",
            "run_id": outcome.run_id,
            "funnel": {
                "chats_with_delta": outcome.chats_with_delta,
                "messages_processed": outcome.messages_processed,
                "actionable": outcome.actionable_chats,
            },
            "notification_status": notif,
            "telegram_text": (
                digest.to_telegram_text() if digest.has_actionable_items else None
            ),
        }
    )
    return rc


def _cmd_scan(
    conn: sqlite3.Connection, config: Config, dry_run: bool, days: int | None
) -> int:
    """Unified pipeline: sync -> Stage 1 -> Stage 2 -> digest -> deliver, with a trace."""
    mode: Mode = "dry_run" if dry_run else "live"
    connector = None if dry_run else _build_connector(config)
    outcome = scan(conn, config, mode=mode, days=days, connector=connector, progress=_progress)

    for chat_id, err in outcome.errors:
        print(f"  ! chat {chat_id} skipped (cursor not advanced): {err}", file=sys.stderr)
    _emit_result(scan_outcome_to_dict(outcome))
    return 1 if outcome.notification_status in ("failed", "offline") else 0


def _cmd_resync(conn: sqlite3.Connection, config: Config) -> int:
    """Incremental upsert from the connector buffer (the Resync action)."""
    connector = _build_connector(config)
    _progress("▶ resync starting — pulling latest from the connector buffer")
    try:
        # Liveness gate (#29): self-heal the sidecar if it merely stopped, else
        # abort loudly instead of silently upserting nothing from a dead source.
        preflight(config, connector, progress=_progress)
    except ConnectorOffline as exc:
        _progress(f"✗ resync aborted — WhatsApp source offline: {exc}")
        send_alert(config, f"⚠️ WhatsApp Radar: resync aborted — source offline ({exc}).")
        _emit_result({"kind": "resync", "ok": False, "error": f"connector offline: {exc}"})
        return 1
    outcome = resync(conn, connector)
    _progress(
        f"✓ resync done — {outcome.chats_added} chats added, "
        f"{outcome.chats_updated} updated, {outcome.messages_added} new messages"
        + (" (no changes)" if outcome.is_noop else "")
    )
    _emit_result(resync_outcome_to_dict(outcome))
    return 0


def _cmd_reprocess(conn: sqlite3.Connection, config: Config, confirm: bool) -> int:
    """Full cache rebuild preserving operator state (the guarded Reprocess action)."""
    if not confirm:
        print(
            "reprocess is destructive (run history resets). Re-run with --confirm.",
            file=sys.stderr,
        )
        return 2
    connector = _build_connector(config)
    _progress("▶ reprocess starting — backing up DB, then rebuilding from the buffer")
    outcome = reprocess(conn, connector, config.db_path)
    _progress(f"  • backed up to {outcome.backup_path}")
    _progress(
        f"✓ reprocess done — {outcome.chats_after} chats / {outcome.messages_after} msgs; "
        f"preserved {outcome.monitored_preserved} monitored, "
        f"{outcome.ignored_preserved} ignored, {outcome.aliases_preserved} aliases"
        + (f"; {len(outcome.unmapped)} unmapped" if outcome.unmapped else "")
    )
    _emit_result(reprocess_outcome_to_dict(outcome))
    return 0


def _deliver(
    conn: sqlite3.Connection, config: Config, run_id: int, digest: Digest
) -> tuple[str, int]:
    """Deliver a run's digest, recording the outcome. Returns (status, exit_code)."""
    status, detail = deliver_digest(conn, config, run_id, digest)
    if status == "failed":
        print(f"Delivery failed (retry with 'wr notify'): {detail}", file=sys.stderr)
        return status, 1
    if status == "skipped":
        print("Notifier is 'none' — digest recorded as skipped (set WR_NOTIFIER=telegram).",
              file=sys.stderr)
        return status, 0
    print(f"Digest delivered via {config.notifier}.", file=sys.stderr)
    return status, 0


def _cmd_notify(conn: sqlite3.Connection, config: Config, run_id: int | None) -> int:
    """Message piece: (re)deliver the digest for a run — the latest by default."""
    rid = run_id if run_id is not None else store.latest_run_id(conn)
    if rid is None:
        print("No review run to deliver. Run 'wr review' first.", file=sys.stderr)
        _emit_result({"kind": "notify", "ok": False, "error": "no run to deliver"})
        return 1
    digest = build_digest(conn, rid)
    if not digest.has_actionable_items:
        _progress(f"notify: run {rid} has no actionable items — nothing to deliver")
        _emit_result(
            {"kind": "notify", "ok": True, "run_id": rid, "notification_status": "none"}
        )
        return 0
    _progress(f"▶ message starting — delivering digest for run {rid}")
    status, rc = _deliver(conn, config, rid, digest)
    _progress(f"✓ message done — notify {status}")
    _emit_result(
        {
            "kind": "notify",
            "ok": status != "failed",
            "run_id": rid,
            "notification_status": status,
            "telegram_text": digest.to_telegram_text(),
        }
    )
    return rc


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

    sub.add_parser(
        "resync", help="incremental upsert of chats/messages from the connector buffer"
    )
    p_reproc = sub.add_parser(
        "reprocess",
        help="rebuild the local cache from the buffer (destructive; preserves operator state)",
    )
    p_reproc.add_argument(
        "--confirm",
        action="store_true",
        help="required: acknowledge that run history resets before rebuilding",
    )

    sub.add_parser("tray", help="run the system-tray surface that owns the admin webapp")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)

    # The tray owns the webapp lifecycle, not the message store — no DB needed.
    if args.command == "tray":
        from app.tray.tray import run_tray

        return run_tray()

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
        if args.command == "resync":
            return _cmd_resync(conn, config)
        if args.command == "reprocess":
            return _cmd_reprocess(conn, config, args.confirm)
    finally:
        conn.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
