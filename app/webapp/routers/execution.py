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
from app.webapp.routers._helpers import db_path, get_conn, maybe_json
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
        connected = bool(statuses) and all(item["connected"] for item in statuses)
        detail = "; ".join(
            f"{item['source']}: {item['detail']}" for item in statuses
        )
        return {
            "name": statuses[0]["name"] if len(statuses) == 1 else "multi-source",
            "connected": connected,
            "detail": detail,
            "sources": statuses,
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


@router.get("/api/execution/runs")
async def list_execution_runs(request: Request, limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    return {"active": runs.active_run(), "runs": runs.list_runs(limit)}


@router.get("/api/execution/runs/{kind}/{run_id}")
async def get_execution_run(request: Request, kind: str, run_id: str) -> dict[str, Any]:
    record = runs.get_run(kind, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": record}


@router.post("/api/execution/runs/{kind}/{run_id}/kill")
async def kill_execution_run(request: Request, kind: str, run_id: str) -> dict[str, Any]:
    if runs.get_run(kind, run_id, with_output=False) is None:
        raise HTTPException(status_code=404, detail="run not found")
    signalled = runs.kill_run(kind, run_id)
    if not signalled:
        raise HTTPException(status_code=409, detail="run is not the active, running run")
    return {"kind": kind, "run_id": run_id, "signalled": True}
