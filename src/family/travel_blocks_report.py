"""Human-readable progress lines for a ``calendar-scan`` travel-block sweep.

Pure formatting over the ``travel_blocks``/``apply`` sections of a
:func:`src.family.calendar_scan.run_calendar_scan` payload — no I/O, no
printing. The caller (``app/cli/main.py``) owns how each returned line is
surfaced (``_progress``), keeping this module reusable from anywhere that
holds the same payload shape (e.g. a future webapp live-log view).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _fmt_block_when(iso: str, *, time_only: bool = False) -> str:
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return moment.strftime("%H:%M" if time_only else "%a %d %b %H:%M")


def format_travel_blocks_progress(section: dict[str, Any]) -> list[str]:
    """Lines for the travel-block plan and what was applied.

    One line per block to add with its origin, destination, time box and
    minutes, one per block to delete *with the reason it is stale*, then every
    leg that could **not** be priced, phrased so it can never be read as "no
    commute was needed", then a counted summary carrying the Routes call count
    and, when the feature is live, what the apply actually did.

    The delete lines are the rehearsal required before any live write is
    allowed: read them on a dry run and confirm nothing outside the plan is
    listed for deletion.
    """
    status = str(section.get("status") or "")
    # `disabled` is a decision, not an unestablished fact, and it is the state
    # of every default install — printing it on every scan would be pure noise.
    # It still rides the run payload. The other non-ok statuses are
    # misconfigurations of an *enabled* feature and stay loud.
    if not status or status == "disabled":
        return []
    lines: list[str] = []
    if status != "ok":
        lines.append(f"🚗 travel blocks: {status} — no plan computed, no Routes calls")
        return lines
    for leg in section.get("adds", []):
        lines.append(
            f"   + {leg['person']} {leg['leg']} — “{leg['event']}”: "
            f"{leg['origin']} → {leg['destination']}, "
            f"{_fmt_block_when(leg['start'])}–{_fmt_block_when(leg['end'], time_only=True)} "
            f"({leg['minutes']} min)"
        )
    for pending in section.get("deletes", []):
        lines.append(
            f"   − delete ({pending['reason']}) {pending['leg']} block {pending['event_id']} "
            f"on {pending['calendar_id']} — source event {pending['source_event_id']}, "
            f"starting {_fmt_block_when(pending['start'])}"
        )
    for failure in section.get("failures", []):
        lines.append(
            f"   ⚠️ unpriced ({failure['reason']}) {failure['person']} {failure['leg']} — "
            f"“{failure['event']}”: {failure['detail']}. No block planned — the drive "
            f"could not be established, this is not a decision that none is needed"
        )
    counts = section.get("counts") or {}
    lines.append(
        f"🚗 travel blocks: {counts.get('adds', 0)} add(s), "
        f"{counts.get('deletes', 0)} delete(s), "
        f"{counts.get('keeps', 0)} kept, "
        f"{counts.get('failures', 0)} unpriced leg(s), "
        f"{section.get('routes_calls', 0)} Routes call(s)"
        + (" [dry-run]" if section.get("dry_run") else "")
    )
    lines.extend(format_travel_block_apply_progress(section.get("apply") or {}))
    return lines


def format_travel_block_apply_progress(apply: dict[str, Any]) -> list[str]:
    """Lines for what the apply actually did, and every calendar it may not write to.

    A calendar whose capability is ``unknown`` gets its own line: it is neither
    a permission nor a refusal, and folding it into either would hide the one
    state that means "we never found out".
    """
    if not apply:
        return []
    lines: list[str] = []
    for calendar_id, state in sorted((apply.get("write_capability") or {}).items()):
        if state == "writable":
            continue
        lines.append(
            f"   ⚠️ calendar {calendar_id}: write capability {state} — nothing written to it"
        )
    for failure in apply.get("failures", []):
        lines.append(
            f"   ⚠️ {failure['operation']} skipped ({failure['reason']}) on "
            f"{failure['calendar_id']}"
            + (f": {failure['detail']}" if failure.get("detail") else "")
        )
    counts = apply.get("counts") or {}
    lines.append(
        f"🚗 travel blocks [{apply.get('status', '?')}]: "
        f"{counts.get('inserted', 0)} inserted, {counts.get('deleted', 0)} deleted, "
        f"{counts.get('kept', 0)} kept, {counts.get('skipped', 0)} skipped, "
        f"{counts.get('backups', 0)} backup(s)"
        + (f" — {apply['detail']}" if apply.get("detail") else "")
    )
    return lines
