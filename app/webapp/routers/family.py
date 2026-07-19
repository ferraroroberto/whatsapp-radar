"""Family rules surface (issues #160, #167): the resolved schedule, editable.

Recent runs come from the unified DB run store (#163) so a scheduled App
Launcher execution is exactly as visible as a webapp-launched one; the Run tab
(#164) is where a check is actually fired and where recent runs now live. This
endpoint exposes the resolved rules/config (non-secret) and lets the webapp
edit the household schedule in place — on-duty weekday pattern, kids-home time,
childcare windows, quiet hours, significant delay, the daily-scan enable toggle
— straight into the gitignored ``config/local.json``. Calendar accounts stay
read-only (provisioned by the calendar-bootstrap flow, not the UI). This gives
the operator full transparency and control over the exact rules in force
instead of a black box or a file edit.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.webapp.routers._helpers import get_conn
from src.config import load_config, save_local_overrides
from src.db import store

router = APIRouter()


class ChildcareWindowIn(BaseModel):
    """A childcare window as edited from the Family tab (#167).

    ``end_time`` is optional — blank keeps the original point-in-time deadline
    semantics (e.g. a pickup); set it to describe a genuine coverage range.
    """

    label: str
    days: list[str]
    time: str
    end_time: str = ""


class FamilyUpdate(BaseModel):
    """The UI-editable subset of the family-check settings (safe, non-secret).

    Extended in #167 to cover the household schedule itself — it is schedule
    data, not a secret, and belongs in ``config/local.json`` like the rest of
    this safe-override subset.
    """

    traffic_enabled: bool | None = None
    family_enabled: bool | None = None
    significant_delay_min: int | None = None
    cadence_min: int | None = None
    run_hour: int | None = None
    quiet_start_hour: int | None = None
    quiet_end_hour: int | None = None
    kids_home_time: str | None = None
    responsible_by_weekday: dict[str, str] | None = None
    childcare_windows: list[ChildcareWindowIn] | None = None


def _hour(value: int) -> int:
    if not 0 <= value <= 23:
        raise HTTPException(status_code=400, detail="hour must be in 0..23")
    return value

_FAMILY_KINDS = {"calendar-scan", "traffic-check"}
_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WEEKDAY_LOOKUP = {name.lower(): name.lower() for name in _WEEKDAY_NAMES}


def _hhmm(value: str, *, what: str) -> tuple[int, int]:
    """Parse ``"HH:MM"``, raising a clear 400 on anything else (#167)."""
    text = (value or "").strip()
    parts = text.split(":")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        hh, mm = int(parts[0]), int(parts[1])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    raise HTTPException(status_code=400, detail=f"{what} must be a valid HH:MM time, got '{value}'")


def _weekday_key(value: str) -> str:
    key = value.strip().lower()
    if key not in _WEEKDAY_LOOKUP:
        raise HTTPException(
            status_code=400, detail=f"'{value}' is not a weekday (Mon..Sun)"
        )
    return key


def _run_summary(row: sqlite3.Row) -> dict[str, Any]:
    """One family run row (#163) distilled for the recent-runs list."""
    try:
        result = json.loads(row["summary_json"]) if row["summary_json"] else {}
    except (ValueError, TypeError):
        result = {}
    kind = row["kind"]
    summary: dict[str, Any] = {
        "kind": kind,
        "run_id": f"db-{row['id']}",
        "status": row["status"],
        "mode": row["mode"],
        "started_at": row["started_at"],
        "finished_at": row["completed_at"],
        "result_status": result.get("status"),
    }
    if kind == "traffic-check":
        summary["checked"] = len(result.get("checked") or [])
        summary["alerts"] = result.get("alerts")
    else:
        summary["conflicts"] = len(result.get("conflicts") or [])
        # Renamed unknown_locations -> missing_locations in #168; old rows persist.
        summary["missing_locations"] = len(
            result.get("missing_locations") or result.get("unknown_locations") or []
        )
    return summary


def _traffic_alerts(row: sqlite3.Row) -> int:
    """Alert count from a traffic-check run's persisted summary (#164)."""
    try:
        result = json.loads(row["summary_json"]) if row["summary_json"] else {}
    except (ValueError, TypeError):
        return 0
    try:
        return int(result.get("alerts") or 0)
    except (ValueError, TypeError):
        return 0


def _family_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    """The rules currently in force plus recent family-check runs."""
    config = load_config()
    traffic, family, calendar = config.traffic, config.family, config.calendar

    responsible = {
        _WEEKDAY_NAMES[day]: person
        for day, person in sorted(family.responsible_by_weekday.items())
        if 0 <= day < 7
    }
    windows = [
        {
            "label": window.label,
            "days": [_WEEKDAY_NAMES[d] for d in window.weekdays if 0 <= d < 7],
            "time": window.time,
            "end_time": window.end_time,
        }
        for window in family.childcare_windows
    ]
    # One newest-first pass over the unified run store (#163) feeds both the
    # recent-runs list and the Run-tab traffic card's status line (#164).
    recent_runs = store.list_review_runs(conn, 200)
    family_runs = [
        _run_summary(row) for row in recent_runs if row["kind"] in _FAMILY_KINDS
    ][:15]
    traffic_rows = [row for row in recent_runs if row["kind"] == "traffic-check"]
    last_check = traffic_rows[0]["started_at"] if traffic_rows else None
    last_alert = next(
        (row["started_at"] for row in traffic_rows if _traffic_alerts(row) > 0), None
    )

    return {
        "traffic": {
            "enabled": traffic.enabled,
            "api_key_set": bool(traffic.api_key),
            "significant_delay_min": traffic.significant_delay_min,
            "cadence_min": traffic.cadence_min,
            "quiet_start_hour": traffic.quiet_start_hour,
            "quiet_end_hour": traffic.quiet_end_hour,
            "dedup_window_min": traffic.dedup_window_min,
            "origin_lookback_min": traffic.origin_lookback_min,
            "lookahead_hours": traffic.lookahead_hours,
            "last_check": last_check,
            "last_alert": last_alert,
        },
        "family": {
            "enabled": family.enabled,
            "run_hour": family.run_hour,
            "home_address": family.home_address,
            "kids_home_time": family.kids_home_time,
            "responsible_by_weekday": responsible,
            "childcare_windows": windows,
            "unknown_scan_days": family.unknown_scan_days,
            "assessment_days": family.assessment_days,
        },
        "calendars": [
            {"person": account.person, "calendar_id": account.calendar_id, "label": account.label}
            for account in calendar.accounts
        ],
        "token_present": calendar.token_path.is_file(),
        "runs": family_runs,
    }


@router.get("/api/family")
async def get_family(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return _family_payload(conn)


@router.post("/api/family")
async def update_family(
    payload: FamilyUpdate, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """Persist the editable subset to the ignored ``config/local.json``.

    Toggles/thresholds plus the household schedule (on-duty weekday pattern,
    kids-home time, childcare windows, quiet hours) are all writable here
    (#167) — home address and calendar accounts stay file-edited, shown
    read-only in the UI. Validation: every time value must parse as HH:MM, a
    submitted on-duty pattern must name exactly the 7 weekdays (a day can map
    to "" for "nobody scheduled"), and a childcare window's optional end must
    come after its start (non-inverted).
    """
    traffic: dict[str, Any] = {}
    family: dict[str, Any] = {}
    if payload.traffic_enabled is not None:
        traffic["enabled"] = payload.traffic_enabled
    if payload.significant_delay_min is not None:
        if not 0 <= payload.significant_delay_min <= 240:
            raise HTTPException(status_code=400, detail="significant_delay_min must be 0..240")
        traffic["significant_delay_min"] = payload.significant_delay_min
    if payload.cadence_min is not None:
        if not 1 <= payload.cadence_min <= 1440:
            raise HTTPException(status_code=400, detail="cadence_min must be 1..1440")
        traffic["cadence_min"] = payload.cadence_min
    if payload.quiet_start_hour is not None:
        traffic["quiet_start_hour"] = _hour(payload.quiet_start_hour)
    if payload.quiet_end_hour is not None:
        traffic["quiet_end_hour"] = _hour(payload.quiet_end_hour)
    if payload.family_enabled is not None:
        family["enabled"] = payload.family_enabled
    if payload.run_hour is not None:
        family["run_hour"] = _hour(payload.run_hour)
    if payload.kids_home_time is not None:
        _hhmm(payload.kids_home_time, what="kids_home_time")
        family["kids_home_time"] = payload.kids_home_time.strip()
    if payload.responsible_by_weekday is not None:
        submitted = {k.strip().lower(): v for k, v in payload.responsible_by_weekday.items()}
        if set(submitted) != set(_WEEKDAY_LOOKUP):
            missing = sorted(set(_WEEKDAY_LOOKUP) - set(submitted))
            extra = sorted(set(submitted) - set(_WEEKDAY_LOOKUP))
            detail = "responsible_by_weekday must name exactly Mon..Sun"
            if missing:
                detail += f" (missing: {', '.join(missing)})"
            if extra:
                detail += f" (unknown: {', '.join(extra)})"
            raise HTTPException(status_code=400, detail=detail)
        family["responsible_by_weekday"] = {
            day: (person or "").strip() for day, person in submitted.items()
        }
    if payload.childcare_windows is not None:
        windows_out: list[dict[str, Any]] = []
        for window in payload.childcare_windows:
            label = window.label.strip()
            if not label:
                raise HTTPException(status_code=400, detail="a childcare window needs a label")
            days = [_weekday_key(d) for d in window.days]
            if not days:
                raise HTTPException(
                    status_code=400, detail=f"childcare window '{label}' needs at least one weekday"
                )
            start_h, start_m = _hhmm(window.time, what=f"childcare window '{label}' time")
            end_time = (window.end_time or "").strip()
            if end_time:
                end_h, end_m = _hhmm(end_time, what=f"childcare window '{label}' end_time")
                if (end_h, end_m) <= (start_h, start_m):
                    raise HTTPException(
                        status_code=400,
                        detail=f"childcare window '{label}' end must be after start (non-inverted)",
                    )
            windows_out.append({
                "label": label, "weekdays": days,
                "time": window.time.strip(), "end_time": end_time,
            })
        family["childcare_windows"] = windows_out

    overrides: dict[str, Any] = {}
    if traffic:
        overrides["traffic"] = traffic
    if family:
        overrides["family"] = family
    if overrides:
        save_local_overrides(overrides)
    return _family_payload(conn)
