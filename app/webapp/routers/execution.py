"""Execution tab (#11): run the end-to-end pipeline — or a single stage — and
watch it live.

This router is a thin launcher over the CLI verbs. Every action maps to a
``python launcher.py <cmd>`` invocation spawned by :mod:`app.webapp.runs`, which
captures the process's combined output to ``output.log`` and parses its final
``__WR_RESULT__`` sentinel into a structured result. The router holds no business
logic: it validates the request, composes argv, and surfaces run records.

The pipeline is exposed both whole and in pieces so the operator can run the full
chain or validate one segment, live or dry:

* ``scan``      — the whole funnel (sync → Stage 1 → Stage 2 → digest → notify),
  live or ``dry_run`` (optionally windowed to the last ``days``).
* ``process``   — analyze monitored deltas on already-synced data (``review``),
  live or ``dry_run``.
* ``notify``    — (re)deliver a run's digest (the message stage).
* ``resync``    — incremental upsert from the connector buffer (no analysis).
* ``reprocess`` — guarded full cache rebuild (preserves operator state).

Only one run executes at a time (they share the SQLite store + connector buffer);
a second request returns 409.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.webapp import runs
from app.webapp.routers._helpers import db_path, get_conn, loads_json_column, maybe_json
from src.config import load_config
from src.connector.factory import build_connectors
from src.connector.gmail import GmailConnector
from src.db import store

router = APIRouter()

# Actions whose argv carries a live/dry-run mode.
_MODAL_ACTIONS = {"scan", "process", "calendar-scan", "traffic-check"}
_VALID_MODES = {"live", "dry_run"}
_DAYS_MAX = 3650  # a generous ceiling; the UI offers small windows


def _compose_argv(action: str, body: dict[str, Any]) -> list[str]:
    """Translate a validated action + options into the launcher argv tail.

    Raises HTTPException(400/403) on bad input so nothing is ever spawned for a
    malformed request.
    """
    mode = str(body.get("mode") or "live")
    if action in _MODAL_ACTIONS and mode not in _VALID_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {sorted(_VALID_MODES)}")

    if action == "scan":
        argv = ["scan"]
        if mode == "dry_run":
            argv.append("--dry-run")
            days = body.get("days")
            if days is not None:
                ok = isinstance(days, int) and not isinstance(days, bool)
                if not ok or not 1 <= days <= _DAYS_MAX:
                    raise HTTPException(status_code=400, detail="days must be an int in 1..3650")
                argv += ["--days", str(days)]
        return argv

    if action == "process":
        argv = ["review"]
        if mode == "dry_run":
            argv.append("--dry-run")
        return argv

    if action == "notify":
        argv = ["notify"]
        run = body.get("run")
        if run is not None:
            if not isinstance(run, int) or isinstance(run, bool) or run < 1:
                raise HTTPException(status_code=400, detail="run must be a positive int")
            argv += ["--run", str(run)]
        return argv

    if action == "resync":
        return ["resync"]

    if action == "reprocess":
        # Destructive: the gate is enforced here too, not just in the UI, so a
        # direct API hit can't skip the acknowledgement.
        if not bool(body.get("confirm")):
            raise HTTPException(
                status_code=403,
                detail="reprocess rebuilds the cache and resets run history — confirm required",
            )
        return ["reprocess", "--confirm"]

    if action in {"calendar-scan", "traffic-check"}:
        # Family checks (#160): the same verb App Launcher Jobs schedule. A live
        # run honours the enable toggle and may send an alert; dry-run never does.
        argv = [action]
        if mode == "dry_run":
            argv.append("--dry-run")
        return argv

    raise HTTPException(status_code=400, detail=f"unknown action {action!r}")


def _calendar_source(cfg: Any, recent_runs: list[sqlite3.Row]) -> dict[str, Any]:
    """A read-only Calendar row for Sources health (#164).

    Calendar is not a message connector — it feeds the family daily scan — so it
    is described from :class:`CalendarConfig` plus the last successful
    ``calendar-scan`` run in the unified store (#163) as its "last fetch".
    """
    calendar = cfg.calendar
    token_present = calendar.token_path.is_file()
    last_success = next(
        (
            row["started_at"]
            for row in recent_runs
            if row["kind"] == "calendar-scan" and row["status"] == "completed"
        ),
        None,
    )
    return {
        "source": "calendar",
        "name": "Google Calendar",
        "enabled": cfg.family.enabled,
        "configured": bool(calendar.accounts) and token_present,
        "authorized": token_present,
        "connected": token_present,
        "detail": "read-only Google Calendar",
        "token_present": token_present,
        "account_count": len(calendar.accounts),
        "accounts": [account.label or account.person for account in calendar.accounts],
        "last_success_at": last_success,
    }


@router.post("/api/execution/run")
async def start_execution_run(request: Request) -> dict[str, Any]:
    body = await maybe_json(request)
    action = str(body.get("action") or "").strip()
    if not action:
        raise HTTPException(status_code=400, detail="action is required")

    argv = _compose_argv(action, body)
    try:
        # Pin the child to the same DB the webapp reads (also keeps tests hermetic).
        started = runs.start_run(
            action, argv, env_overrides={"WR_DB_PATH": str(db_path(request))}
        )
    except runs.RunBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"spawn failed: {exc}") from exc
    return started


@router.get("/api/execution/health")
async def execution_health(request: Request) -> dict[str, Any]:
    """Truthful, secret-free status for every configured message source."""
    cfg = load_config()
    conn = store.connect(db_path(request))
    try:
        aggregates = {row["source"]: dict(row) for row in store.source_overview(conn)}
        syncs = [dict(row) for row in store.recent_syncs(conn, 100)]
        statuses = []
        for binding in build_connectors(cfg):
            try:
                status = binding.connector.connect()
                account = None
                if isinstance(binding.connector, GmailConnector) and status.connected:
                    profile = binding.connector.profile()
                    account = profile.masked_email_address
            except Exception as exc:
                status = binding.connector.status()
                status = type(status)(
                    status.name,
                    False,
                    f"status probe failed ({type(exc).__name__})",
                )
                account = None
            finally:
                binding.connector.stop()

            source_syncs = [row for row in syncs if row["connector_source"] == binding.source]
            last_attempt = source_syncs[0] if source_syncs else None
            last_success = next(
                (row for row in source_syncs if row["status"] == "success"), None
            )
            stored = aggregates.get(binding.source, {})
            item: dict[str, Any] = {
                "source": binding.source,
                "name": status.name,
                "enabled": binding.source in cfg.sources,
                "configured": (
                    bool(cfg.gmail.senders or cfg.gmail.labels)
                    and cfg.gmail.token_path.is_file()
                    if binding.source == "gmail"
                    else True
                ),
                "authorized": status.connected,
                "connected": status.connected,
                "detail": status.detail,
                "stored_channels": int(stored.get("channels") or 0),
                "stored_messages": int(stored.get("messages") or 0),
                "monitored_channels": int(stored.get("monitored") or 0),
                "latest_stored_at": stored.get("latest_message_at"),
                "last_attempt": last_attempt,
                "last_success": last_success,
            }
            if binding.source == "gmail":
                whitelist = {
                    "senders": [
                        {"address": sender.address, "name": sender.name}
                        for sender in cfg.gmail.senders
                    ],
                    "labels": [
                        {"name": label.name, "display_name": label.display_name}
                        for label in cfg.gmail.labels
                    ],
                }
                item.update(
                    {
                        "read_only": True,
                        "account": account,
                        "token_present": cfg.gmail.token_path.is_file(),
                        "whitelist": whitelist,
                        "whitelist_valid": status.connected,
                        "history_scope": "All messages matching the whitelist; no lookback limit.",
                    }
                )
            statuses.append(item)
        # Overall health reflects the *message* sources only; the calendar row is
        # a read-only, non-ingesting source appended for visibility (#164).
        connected = bool(statuses) and all(item["connected"] for item in statuses)
        detail = "; ".join(
            f"{item['source']}: {item['detail']}" for item in statuses
        )
        sources = [*statuses, _calendar_source(cfg, store.list_review_runs(conn, 200))]
        return {
            "name": statuses[0]["name"] if len(statuses) == 1 else "multi-source",
            "connected": connected,
            "detail": detail,
            "sources": sources,
        }
    except ValueError as exc:
        return {
            "name": "multi-source",
            "connected": False,
            "detail": str(exc),
            "sources": [],
        }
    finally:
        conn.close()


@router.get("/api/execution/syncs")
async def list_syncs(
    limit: int = 20,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Recent sync deltas + current stored totals (the 'is it working?' view).

    Every sync path writes a ``sync_log`` row, so this lists when each ran and how
    many chats/messages it added — covering scheduled Jobs the run viewer can't
    see. ``totals`` are the live counts so the operator can confirm the store is
    actually growing.
    """
    limit = max(1, min(limit, 100))
    rows = [dict(r) for r in store.recent_syncs(conn, limit)]
    by_source = {
        row["source"]: {
            "channels": int(row["channels"]),
            "monitored": int(row["monitored"]),
            "messages": int(row["messages"]),
            "latest_message_at": row["latest_message_at"],
        }
        for row in store.source_overview(conn)
    }
    totals = {
        "chats": store.count_chats(conn),
        "messages": store.message_count_total(conn),
        "by_source": by_source,
    }
    return {"syncs": rows, "totals": totals}


def _fs_mode(record: dict[str, Any]) -> str:
    argv = record.get("argv") or []
    return "dry_run" if "--dry-run" in argv else "live"


def _db_run_record(row: sqlite3.Row) -> dict[str, Any]:
    """Shape a DB run row as a run record for the unified runs list/viewer (#163).

    Covers runs launched outside the webapp (CLI, App Launcher Jobs) which have
    no filesystem record: family checks carry their summary payload as the
    result; scan/process rows rebuild the funnel from the DB columns.
    """
    rid = int(row["id"])
    kind = row["kind"]
    if kind in ("calendar-scan", "traffic-check"):
        result = loads_json_column(row["summary_json"])
    else:
        result = {
            "kind": kind,
            "run_id": rid,
            "funnel": {
                "messages_synced": int(row["messages_synced"]),
                "chats_monitored": int(row["chats_monitored"]),
                "chats_with_delta": int(row["chats_reviewed"]),
                "transcriptions": int(row["transcriptions"]),
                "stage1_passed": int(row["stage1_passed"]),
                "stage2_llm_calls": int(row["stage2_llm_calls"]),
                "actionable": int(row["actionable"]),
            },
            "notification_status": row["notification_status"],
            "sources": loads_json_column(row["source_funnel_json"]) or {},
        }
    return {
        "kind": kind,
        "run_id": f"db-{rid}",
        "db_run_id": rid,
        "origin": "db",
        "status": row["status"],
        "mode": row["mode"],
        "started_at": row["started_at"],
        "finished_at": row["completed_at"],
        "error": row["error"],
        "result": result,
        "output_tail": "(launched outside the webapp — no captured output; "
        "the run record above is the full outcome)",
    }


@router.get("/api/execution/runs")
async def list_execution_runs(
    request: Request,
    limit: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Unified recent runs: one entry per execution across both stores (#163).

    Filesystem records (webapp-launched, carry live output) are merged with DB
    run rows (every launch) by the DB run id; DB-only rows — scheduled scans and
    family checks — synthesize a record so nothing that ran is invisible here.
    """
    limit = max(1, min(limit, 200))
    merged: list[dict[str, Any]] = []
    by_db_id: dict[int, dict[str, Any]] = {}
    for record in runs.list_runs(limit):
        record.setdefault("mode", _fs_mode(record))
        db_id = record.get("db_run_id")
        if not isinstance(db_id, int):
            result = record.get("result")
            db_id = result.get("run_id") if isinstance(result, dict) else None
        if isinstance(db_id, int):
            record["db_run_id"] = db_id
            by_db_id[db_id] = record
        merged.append(record)
    for row in store.list_review_runs(conn, limit):
        rid = int(row["id"])
        matched = by_db_id.get(rid)
        if matched is not None:
            # The DB row is authoritative for timing (one clock, UTC) and — for
            # scan runs — the live/dry mode; process rows keep the argv-derived
            # mode ('review' in the DB conflates the two).
            matched["started_at"] = row["started_at"]
            if row["mode"] in ("live", "dry_run"):
                matched["mode"] = row["mode"]
            continue
        merged.append(_db_run_record(row))
    merged.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return {"active": runs.active_run(), "runs": merged[:limit]}


@router.get("/api/execution/runs/{kind}/{run_id}")
async def get_execution_run(
    request: Request,
    kind: str,
    run_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if run_id.startswith("db-"):
        try:
            rid = int(run_id[3:])
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        row = store.review_run(conn, rid)
        if row is None or row["kind"] != kind:
            raise HTTPException(status_code=404, detail="run not found")
        return {"run": _db_run_record(row)}
    record = runs.get_run(kind, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    record.setdefault("mode", _fs_mode(record))
    return {"run": record}


@router.post("/api/execution/runs/{kind}/{run_id}/kill")
async def kill_execution_run(request: Request, kind: str, run_id: str) -> dict[str, Any]:
    if runs.get_run(kind, run_id, with_output=False) is None:
        raise HTTPException(status_code=404, detail="run not found")
    signalled = runs.kill_run(kind, run_id)
    if not signalled:
        raise HTTPException(status_code=409, detail="run is not the active, running run")
    return {"kind": kind, "run_id": run_id, "signalled": True}
