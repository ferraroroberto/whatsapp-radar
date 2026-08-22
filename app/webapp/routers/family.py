"""Family rules surface (issues #160, #167): the resolved schedule, editable.

Recent runs come from the unified DB run store (#163) so a scheduled App
Launcher execution is exactly as visible as a webapp-launched one; the Run tab
(#164) is where a check is actually fired and where recent runs now live. This
endpoint exposes the resolved rules/config (non-secret) and lets the webapp
edit the household schedule in place — on-duty weekday pattern, kids-home time,
childcare windows, quiet hours, significant delay, the daily-scan enable toggle,
the train-commute leave-now exemption (#227), the commute travel-block knobs
(#268) — straight into the gitignored
``config/local.json``. Calendar accounts stay read-only (provisioned by the
calendar-bootstrap flow, not the UI). This gives
the operator full transparency and control over the exact rules in force
instead of a black box or a file edit.

The travel-block section (#268, closing umbrella #263) is *reported*, never
recomputed here: its per-calendar write capability and last-sweep counts are
read off the newest ``calendar-scan`` run already being listed for the
recent-runs card. Rendering this tab must never trigger a sweep — a sweep
spends Routes quota and, out of dry run, writes to real calendars.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.webapp.routers._helpers import get_conn
from src.config import Config, load_config, save_local_overrides
from src.db import store
from src.family.travel_blocks_write import (
    NOT_WRITABLE,
    WRITABLE,
    WRITE_CAPABILITY_UNKNOWN,
)

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
    ask_missing_locations: bool | None = None
    skip_leave_now_for_train: bool | None = None
    significant_delay_min: int | None = None
    cadence_min: int | None = None
    run_hour: int | None = None
    quiet_start_hour: int | None = None
    quiet_end_hour: int | None = None
    kids_home_time: str | None = None
    responsible_by_weekday: dict[str, str] | None = None
    childcare_windows: list[ChildcareWindowIn] | None = None
    # Commute travel blocks (#268). `horizon_days` is deliberately *not* here:
    # widening it past the days the daily scan fetches is silently clamped
    # (see `_warn_if_horizon_clamped`), so it stays a file edit made with the
    # neighbouring `family.unknown_scan_days` in view rather than a phone tap.
    travel_blocks_enabled: bool | None = None
    travel_blocks_dry_run: bool | None = None
    min_home_dwell_min: int | None = None
    title_template: str | None = None


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


def _summary_json(row: sqlite3.Row) -> dict[str, Any]:
    """A run row's persisted payload, or ``{}`` when it has none / is unreadable.

    One parser for all three readers of the column (the recent-runs list, the
    traffic alert count, the travel-block section) so a malformed payload
    degrades identically everywhere instead of raising in whichever one grew
    its own ``json.loads``.
    """
    try:
        result = json.loads(row["summary_json"]) if row["summary_json"] else {}
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _run_summary(row: sqlite3.Row) -> dict[str, Any]:
    """One family run row (#163) distilled for the recent-runs list."""
    result = _summary_json(row)
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
    return _int(_summary_json(row).get("alerts"))


def _int(value: Any) -> int:
    """A persisted count coerced to an int; anything unusable reads as 0."""
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


# ------------------------------------------------------------- travel blocks

#: How long a block title may be. Not a Google limit — a calendar title that
#: does not fit a phone's month view is useless, and the template is the only
#: text this feature ever puts on a shared calendar.
_MAX_TITLE_TEMPLATE = 60
_CAPABILITY_STATES = frozenset({WRITABLE, NOT_WRITABLE, WRITE_CAPABILITY_UNKNOWN})
#: Stand-in for a *status* (a sweep's, an apply's) the payload does not name.
#: Deliberately not the write-capability constant, which answers a different
#: question and must not become a general-purpose "dunno".
_STATUS_UNKNOWN = "unknown"


def _capability_state(value: Any) -> str:
    """Normalise a persisted capability to one of the three known states.

    Anything else — absent, ``None``, a state from a future version — is
    :data:`WRITE_CAPABILITY_UNKNOWN`, never :data:`WRITABLE`. An unreadable
    answer is an unestablished fact, and an unestablished fact is reported as
    its own state rather than folded into the passing one.
    """
    return str(value) if value in _CAPABILITY_STATES else WRITE_CAPABILITY_UNKNOWN


def _last_travel_sweep(
    recent_runs: Sequence[sqlite3.Row],
) -> tuple[sqlite3.Row, dict[str, Any]] | None:
    """Newest ``calendar-scan`` run that actually carries a travel-block section.

    Runs recorded before #266 have no such section at all; skipping them shows
    the last sweep there really was instead of an empty one. ``started_at``
    rides along in the summary so a stale answer is visibly stale.
    """
    for row in recent_runs:
        if row["kind"] != "calendar-scan":
            continue
        section = _summary_json(row).get("travel_blocks")
        if isinstance(section, dict):
            return row, section
    return None


def _apply_summary(section: dict[str, Any]) -> dict[str, Any] | None:
    """The sweep's write outcome, or ``None`` when it never reached one."""
    applied = section.get("apply")
    if not isinstance(applied, dict):
        return None
    counts = applied.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    failures = applied.get("failures")
    return {
        "status": str(applied.get("status") or _STATUS_UNKNOWN),
        "counts": {
            key: _int(counts.get(key))
            for key in ("inserted", "deleted", "kept", "skipped", "backups")
        },
        "failures": len(failures) if isinstance(failures, list) else 0,
    }


def _sweep_summary(row: sqlite3.Row, section: dict[str, Any]) -> dict[str, Any]:
    """The compact last-sweep view the Family tab renders.

    ``dry_run`` is ``None`` — not ``False`` — for a gated sweep (``disabled`` /
    ``no_routes_api_key`` / ``no_home_address``), which never records one. A
    default of ``False`` there would advertise a live sweep that never ran.
    ``counts`` is ``None`` for the same reason: zeros would read like a
    computed all-clear rather than "nothing was computed".
    """
    counts = section.get("counts")
    return {
        "run_id": f"db-{row['id']}",
        "started_at": row["started_at"],
        "status": str(section.get("status") or _STATUS_UNKNOWN),
        "dry_run": section["dry_run"] if isinstance(section.get("dry_run"), bool) else None,
        "routes_calls": _int(section.get("routes_calls")),
        "counts": (
            {
                key: _int(counts.get(key))
                for key in ("desired", "adds", "deletes", "keeps", "protected", "failures")
            }
            if isinstance(counts, dict)
            else None
        ),
        "apply": _apply_summary(section),
    }


def _travel_blocks_payload(
    config: Config, recent_runs: Sequence[sqlite3.Row]
) -> dict[str, Any]:
    """Travel-block knobs, per-calendar write capability, and the last sweep.

    Composed from the config already loaded plus the run list already read —
    no second store query, and emphatically no sweep.
    """
    settings = config.family.travel_blocks
    calendar = config.calendar
    found = _last_travel_sweep(recent_runs)
    section = found[1] if found else {}
    applied = section.get("apply")
    capability = applied.get("write_capability") if isinstance(applied, dict) else None
    capability = capability if isinstance(capability, dict) else {}
    return {
        "enabled": settings.enabled,
        "dry_run": settings.dry_run,
        "horizon_days": settings.horizon_days,
        "min_home_dwell_min": settings.min_home_dwell_min,
        "title_template": settings.title_template,
        "max_title_template": _MAX_TITLE_TEMPLATE,
        # Mirrors `token_present` for the read-only token: a write token that is
        # not there is why a live sweep would write nothing.
        "write_token_present": calendar.write_token_path.is_file(),
        # One entry per configured calendar, always — a person the last sweep
        # never reported on is `unknown`, never dropped from the list.
        "write_capability": [
            {
                "person": account.person,
                "label": account.label or account.person,
                "calendar_id": account.calendar_id,
                "state": _capability_state(capability.get(account.calendar_id)),
            }
            for account in calendar.accounts
        ],
        # Duplicate `calendar_id` entries collapsed at config-parse time
        # (#273), by label — never the raw calendar id. Empty on every
        # household config that has no such collision. No dedicated UI card
        # yet (deferred, see PR body); this is the reportable state itself.
        "duplicate_calendars": list(calendar.collapsed_duplicate_labels),
        "last_sweep": _sweep_summary(found[0], section) if found else None,
    }


def _travel_block_overrides(payload: FamilyUpdate) -> dict[str, Any]:
    """The submitted travel-block knobs, validated, ready to deep-merge.

    Range/blank checks name the offending field in the 400, following the
    ``significant_delay_min`` / ``cadence_min`` precedent below: a rejected save
    has to say which control to fix, since the tab posts several at once.
    """
    out: dict[str, Any] = {}
    if payload.travel_blocks_enabled is not None:
        out["enabled"] = payload.travel_blocks_enabled
    if payload.travel_blocks_dry_run is not None:
        out["dry_run"] = payload.travel_blocks_dry_run
    if payload.min_home_dwell_min is not None:
        # 0 is legitimate (always write the return-home leg); 480 is half a day,
        # past which no same-day gap could ever clear the threshold.
        if not 0 <= payload.min_home_dwell_min <= 480:
            raise HTTPException(status_code=400, detail="min_home_dwell_min must be 0..480")
        out["min_home_dwell_min"] = payload.min_home_dwell_min
    if payload.title_template is not None:
        title = payload.title_template.strip()
        if not title:
            raise HTTPException(status_code=400, detail="title_template must not be blank")
        if len(title) > _MAX_TITLE_TEMPLATE:
            raise HTTPException(
                status_code=400,
                detail=f"title_template must be at most {_MAX_TITLE_TEMPLATE} characters",
            )
        out["title_template"] = title
    return out


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
            # What the check actually applies — may be higher than configured,
            # since the window is floored at the lookahead (#252). Reporting only
            # the configured value would advertise a window that is not in force.
            "effective_dedup_window_min": traffic.effective_dedup_window_min,
            "origin_lookback_min": traffic.origin_lookback_min,
            "lookahead_hours": traffic.lookahead_hours,
            "skip_leave_now_for_train": traffic.skip_leave_now_for_train,
            "train_keywords": list(traffic.train_keywords),
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
            "ask_missing_locations": family.ask_missing_locations,
        },
        "calendars": [
            {"person": account.person, "calendar_id": account.calendar_id, "label": account.label}
            for account in calendar.accounts
        ],
        "token_present": calendar.token_path.is_file(),
        # Reuses `recent_runs` above — the tab is rendered from one store read
        # and never fires a sweep of its own (#268).
        "travel_blocks": _travel_blocks_payload(config, recent_runs),
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
    kids-home time, childcare windows, quiet hours) and the travel-block knobs
    (#268) are all writable here — home address and calendar accounts stay
    file-edited, shown read-only in the UI. Validation: every time value must
    parse as HH:MM, a submitted on-duty pattern must name exactly the 7
    weekdays (a day can map to "" for "nobody scheduled"), a childcare window's
    optional end must come after its start (non-inverted), and the travel-block
    dwell/title must be in range and non-blank.
    """
    traffic: dict[str, Any] = {}
    family: dict[str, Any] = {}
    if payload.traffic_enabled is not None:
        traffic["enabled"] = payload.traffic_enabled
    if payload.skip_leave_now_for_train is not None:
        traffic["skip_leave_now_for_train"] = payload.skip_leave_now_for_train
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
    if payload.ask_missing_locations is not None:
        family["ask_missing_locations"] = payload.ask_missing_locations
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

    travel_blocks = _travel_block_overrides(payload)
    if travel_blocks:
        family["travel_blocks"] = travel_blocks

    overrides: dict[str, Any] = {}
    if traffic:
        overrides["traffic"] = traffic
    if family:
        overrides["family"] = family
    if overrides:
        save_local_overrides(overrides)
    return _family_payload(conn)
