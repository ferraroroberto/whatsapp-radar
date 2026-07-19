"""Command-line entry point.

Commands: status | ingest | chats | monitor | ignore | review | scan | notify |
resync | reprocess | gmail-survey. The CLI wires the boundaries together but holds no business
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
from datetime import UTC, datetime

from gmail_readonly import GmailReadError

from src.analysis.classifier import build_classifier
from src.analysis.gmail_survey import run_gmail_survey
from src.analysis.pipeline import Mode, scan, scan_outcome_to_dict
from src.analysis.review import review_monitored_chats
from src.analysis.source_funnel import source_funnels_dict, source_funnels_json
from src.config import Config, load_config
from src.connector.factory import ConnectorBinding, build_connectors
from src.connector.preflight import ConnectorOffline, preflight
from src.db import store
from src.db.reprocess import reprocess, reprocess_outcome_to_dict
from src.db.sync import resync, resync_outcome_to_dict, sync_sources
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


def _build_connectors(config: Config) -> list[ConnectorBinding]:
    try:
        return build_connectors(config)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _cmd_status(conn: sqlite3.Connection, config: Config) -> int:
    bindings = _build_connectors(config)
    chats = store.list_chats(conn)
    monitored = sum(1 for c in chats if c["status"] == "monitored")
    print(f"DB:         {config.db_path}")
    for binding in bindings:
        cstatus = binding.connector.connect()
        binding.connector.stop()
        print(
            f"Source:     {binding.source} / {cstatus.name} "
            f"(connected={cstatus.connected}) — {cstatus.detail}"
        )
    print(f"Classifier: {config.classifier}")
    print(f"Chats:      {len(chats)} discovered, {monitored} monitored")
    return 0


def _cmd_ingest(conn: sqlite3.Connection, config: Config) -> int:
    synced = sync_sources(conn, _build_connectors(config), operation="ingest")
    delta = synced.delta
    print(f"Ingested {delta.chats_seen} chats, {delta.messages_added} new messages.")
    for source, error in synced.source_errors:
        print(f"  ! {source} failed: {error}", file=sys.stderr)
    return 1 if synced.source_errors else 0


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
        conn, classifier, since_days=config.hub.recent_alert_days, config=config
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

    store.record_run_funnel(
        conn,
        outcome.run_id,
        chats_synced=0,
        messages_synced=0,
        chats_monitored=sum(
            funnel.monitored_channels for funnel in outcome.source_funnels.values()
        ),
        stage1_passed=sum(
            funnel.stage1_passed for funnel in outcome.source_funnels.values()
        ),
        stage2_llm_calls=sum(funnel.llm_calls for funnel in outcome.source_funnels.values()),
        actionable=outcome.actionable_chats,
        notification_status=notif,
        source_funnel_json=source_funnels_json(outcome.source_funnels),
    )

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
            "sources": source_funnels_dict(outcome.source_funnels),
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
    connectors = None if dry_run else _build_connectors(config)
    outcome = scan(
        conn,
        config,
        mode=mode,
        days=days,
        connectors=connectors,
        progress=_progress,
    )

    for chat_id, err in outcome.errors:
        print(f"  ! chat {chat_id} skipped (cursor not advanced): {err}", file=sys.stderr)
    _emit_result(scan_outcome_to_dict(outcome))
    return (
        1
        if outcome.notification_status in ("failed", "offline") or outcome.source_errors
        else 0
    )


def _cmd_resync(conn: sqlite3.Connection, config: Config) -> int:
    """Incremental upsert from the connector buffer (the Resync action)."""
    connectors = _build_connectors(config)
    _progress("▶ resync starting — pulling latest from the connector buffer")
    try:
        # Liveness gate (#29): self-heal the sidecar if it merely stopped, else
        # abort loudly instead of silently upserting nothing from a dead source.
        outcome = resync(
            conn,
            connectors,
            prepare=lambda source, connector: preflight(
                config,
                connector,
                source=source,
                progress=_progress,
            ),
            gmail_retention_days=config.gmail.retention_days,
        )
    except ConnectorOffline as exc:
        _progress(f"✗ resync aborted — all enabled sources offline: {exc}")
        send_alert(config, f"⚠️ WhatsApp Radar: resync aborted — source offline ({exc}).")
        _emit_result({"kind": "resync", "ok": False, "error": f"connector offline: {exc}"})
        return 1
    _progress(
        f"✓ resync done — {outcome.chats_added} chats added, "
        f"{outcome.chats_updated} updated, {outcome.messages_added} new messages"
        + (" (no changes)" if outcome.is_noop else "")
    )
    for source, error in outcome.source_errors:
        _progress(f"⚠ {source} sync failed — {error}; its cursors were not advanced")
    _emit_result(resync_outcome_to_dict(outcome))
    return 1 if outcome.source_errors else 0


def _cmd_reprocess(conn: sqlite3.Connection, config: Config, confirm: bool) -> int:
    """Full cache rebuild preserving operator state (the guarded Reprocess action)."""
    if not confirm:
        print(
            "reprocess is destructive (run history resets). Re-run with --confirm.",
            file=sys.stderr,
        )
        return 2
    connectors = _build_connectors(config)
    _progress("▶ reprocess starting — backing up DB, then rebuilding from the buffer")
    outcome = reprocess(
        conn,
        connectors,
        config.db_path,
        prepare=lambda source, connector: preflight(
            config,
            connector,
            source=source,
            progress=_progress,
        ),
    )
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

    p_survey = sub.add_parser(
        "gmail-survey",
        help="propose Gmail taxonomy/rules from a bounded whitelisted sample",
    )
    p_survey.add_argument(
        "--days", type=int, default=60, help="bounded Gmail lookback window (default: 60)"
    )
    p_survey.add_argument(
        "--max-messages",
        type=int,
        default=100,
        help="maximum full email bodies sent to the local hub (default: 100)",
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

    p_cal = sub.add_parser(
        "calendar-scan", help="daily family calendar-conflict scan (#160)"
    )
    p_cal.add_argument(
        "--dry-run",
        action="store_true",
        help="run fully but never send an alert (the run row itself is recorded)",
    )
    p_traffic = sub.add_parser(
        "traffic-check", help="traffic-jam check for upcoming commutes (#160)"
    )
    p_traffic.add_argument(
        "--dry-run",
        action="store_true",
        help="run fully but never send an alert (the run row itself is recorded)",
    )
    sub.add_parser("tray", help="run the system-tray surface that owns the admin webapp")
    return parser


def _traffic_cadence_skip_reason(conn: sqlite3.Connection, config: Config) -> str | None:
    """Cadence self-skip (#170) — the reason to skip a ``traffic-check`` fire, or None.

    The Windows job is armed at a fixed high frequency (every few minutes) so a
    ``traffic.cadence_min`` edit in the Run tab takes effect immediately, with no
    App Launcher re-arm. Most fires are then no-ops: this compares "now" against
    the last recorded ``traffic-check`` run and skips when the configured cadence
    hasn't elapsed. A skip is deliberately **not** recorded as a run row (it would
    drown the Audit tab in a no-op entry every few minutes); it only prints a log
    line, so `output.log` on the App Launcher side is the skip's audit trail.
    """
    last_started = store.last_run_started_at(conn, "traffic-check")
    if last_started is None:
        return None
    last_dt = datetime.fromisoformat(last_started)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=UTC)
    elapsed_min = (datetime.now(UTC) - last_dt).total_seconds() / 60
    cadence_min = config.traffic.cadence_min
    if elapsed_min < cadence_min:
        return (
            f"cadence {cadence_min}min not elapsed "
            f"({elapsed_min:.1f}min since last check)"
        )
    return None


def _cmd_family_check(
    conn: sqlite3.Connection, config: Config, kind: str, dry_run: bool
) -> int:
    """Run one family check, recording it as a run row like any other kind (#163).

    The run record is what makes a scheduled (App Launcher) execution visible in
    the Audit tab — the check itself never touches the message store.
    """
    import json

    from src.family.calendar_scan import run_calendar_scan
    from src.family.traffic_check import run_traffic_check

    if kind == "traffic-check":
        skip_reason = _traffic_cadence_skip_reason(conn, config)
        if skip_reason is not None:
            _progress(f"⏭ traffic-check skipped — {skip_reason}")
            _emit_result({"kind": kind, "status": "skipped", "reason": skip_reason})
            return 0

    run_id = store.start_run(conn, mode="dry_run" if dry_run else "live", kind=kind)
    runner = run_calendar_scan if kind == "calendar-scan" else run_traffic_check
    try:
        payload = runner(config, now=datetime.now().astimezone(), dry_run=dry_run)
    except (FileNotFoundError, RuntimeError) as exc:
        _progress(f"❌ {kind} failed: {exc}")
        store.finish_run_summary(conn, run_id, "failed", None, error=str(exc))
        _emit_result({"kind": kind, "status": "error", "error": str(exc), "run_id": run_id})
        return 1
    payload["run_id"] = run_id
    if kind == "calendar-scan":
        _progress(
            f"📅 calendar-scan: {payload['status']} — "
            f"{len(payload.get('conflicts', []))} conflict(s), "
            f"{len(payload.get('missing_locations', []))} missing location(s)"
            + (" [dry-run]" if dry_run else "")
        )
    else:
        _progress(
            f"🚗 traffic-check: {payload['status']} — "
            f"{len(payload.get('checked', []))} route(s) checked, "
            f"{payload.get('alerts', 0)} alert(s)"
            + (" [dry-run]" if dry_run else "")
        )
    status = "failed" if payload.get("status") == "error" else "completed"
    store.finish_run_summary(
        conn,
        run_id,
        status,
        json.dumps(payload, ensure_ascii=False),
        error=payload.get("error"),
    )
    _emit_result(payload)
    return 1 if status == "failed" else 0


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
    if args.command == "gmail-survey":
        try:
            run_gmail_survey(
                config,
                days=args.days,
                max_messages=args.max_messages,
                progress=_progress,
            )
        except (FileNotFoundError, GmailReadError, RuntimeError, ValueError) as exc:
            print(f"Gmail survey failed: {exc}", file=sys.stderr)
            return 1
        return 0

    conn = store.connect(config.db_path)
    try:
        # Family checks (#160) never touch the message store, but since #163 they
        # record a run row so scheduled executions are visible in the Audit tab.
        if args.command in ("calendar-scan", "traffic-check"):
            return _cmd_family_check(conn, config, args.command, args.dry_run)
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
