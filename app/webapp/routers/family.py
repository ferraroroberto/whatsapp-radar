"""Family checks surface (issue #160): current rules + recent runs, read-only.

Reuses the generic filesystem run store (``app.webapp.runs``) for history, so
this endpoint only has to expose the resolved rules/config (non-secret) plus the
recent ``calendar-scan`` / ``traffic-check`` runs. Editing the enable toggles and
thresholds happens through the existing Config form; running a check happens
through the Execution tab. This gives the operator full transparency — the exact
rules in force and why each run did what — instead of a black box.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.webapp import runs
from src.config import load_config, save_local_overrides

router = APIRouter()


class FamilyUpdate(BaseModel):
    """The UI-editable subset of the family-check settings (safe, non-secret)."""

    traffic_enabled: bool | None = None
    family_enabled: bool | None = None
    significant_delay_min: int | None = None
    run_hour: int | None = None
    quiet_start_hour: int | None = None
    quiet_end_hour: int | None = None


def _hour(value: int) -> int:
    if not 0 <= value <= 23:
        raise HTTPException(status_code=400, detail="hour must be in 0..23")
    return value

_FAMILY_KINDS = {"calendar-scan", "traffic-check"}
_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _run_summary(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result") or {}
    summary: dict[str, Any] = {
        "kind": record.get("kind"),
        "run_id": record.get("run_id"),
        "status": record.get("status"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "result_status": result.get("status"),
    }
    if record.get("kind") == "traffic-check":
        summary["checked"] = len(result.get("checked") or [])
        summary["alerts"] = result.get("alerts")
    else:
        summary["conflicts"] = len(result.get("conflicts") or [])
        summary["unknown_locations"] = len(result.get("unknown_locations") or [])
    return summary


@router.get("/api/family")
async def get_family() -> dict[str, Any]:
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
        }
        for window in family.childcare_windows
    ]
    family_runs = [
        _run_summary(record)
        for record in runs.list_runs(limit=100)
        if record.get("kind") in _FAMILY_KINDS
    ][:15]

    return {
        "traffic": {
            "enabled": traffic.enabled,
            "api_key_set": bool(traffic.api_key),
            "significant_delay_min": traffic.significant_delay_min,
            "quiet_start_hour": traffic.quiet_start_hour,
            "quiet_end_hour": traffic.quiet_end_hour,
            "dedup_window_min": traffic.dedup_window_min,
            "origin_lookback_min": traffic.origin_lookback_min,
            "lookahead_hours": traffic.lookahead_hours,
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


@router.post("/api/family")
async def update_family(payload: FamilyUpdate) -> dict[str, Any]:
    """Persist the editable subset to the ignored ``config/local.json``.

    Only the safe toggles/thresholds are writable here; the personal schedule
    (addresses, responsibility pattern, childcare windows) is file-edited, shown
    read-only in the UI — mirroring how the message pipeline's keyword roots and
    prompts are read-only in the app.
    """
    traffic: dict[str, Any] = {}
    family: dict[str, Any] = {}
    if payload.traffic_enabled is not None:
        traffic["enabled"] = payload.traffic_enabled
    if payload.significant_delay_min is not None:
        if not 0 <= payload.significant_delay_min <= 240:
            raise HTTPException(status_code=400, detail="significant_delay_min must be 0..240")
        traffic["significant_delay_min"] = payload.significant_delay_min
    if payload.quiet_start_hour is not None:
        traffic["quiet_start_hour"] = _hour(payload.quiet_start_hour)
    if payload.quiet_end_hour is not None:
        traffic["quiet_end_hour"] = _hour(payload.quiet_end_hour)
    if payload.family_enabled is not None:
        family["enabled"] = payload.family_enabled
    if payload.run_hour is not None:
        family["run_hour"] = _hour(payload.run_hour)

    overrides: dict[str, Any] = {}
    if traffic:
        overrides["traffic"] = traffic
    if family:
        overrides["family"] = family
    if overrides:
        save_local_overrides(overrides)
    return await get_family()
