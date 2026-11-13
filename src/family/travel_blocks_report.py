"""The one renderer of a ``calendar-scan`` travel-block plan, for the log and the CLI.

Pure formatting over the serialized plan (:meth:`TravelBlockPlan.to_payload`, the
same dict the run payload persists) and its ``apply`` section: no I/O, no
printing. :func:`plan_lines` is the single line builder, so the log written
while a sweep runs and the lines ``app/cli/main.py`` prints from the persisted
payload cannot disagree about what was planned, left alone or not priced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.family.travel_blocks_model import WRITABLE
from src.family.travel_blocks_pricing import STATUS_DISABLED, STATUS_OK
from src.family.travel_blocks_reconcile import _PROTECT_DETAIL

_UNSPECIFIED = "?"
_NO_DETAIL = "nothing this sweep established says it is stale"


@dataclass(frozen=True)
class PlanLine:
    """One rendered line and the log level it deserves."""

    level: int
    text: str


def _fmt_block_when(iso: str, *, time_only: bool = False) -> str:
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return moment.strftime("%H:%M" if time_only else "%a %d %b %H:%M")


def _block_ref(entry: dict[str, Any]) -> str:
    """The existing-block half of a delete / left-alone line, naming it for audit."""
    start = entry.get("start")
    return (
        f"{entry.get('leg') or _UNSPECIFIED} block {entry.get('event_id') or _UNSPECIFIED} "
        f"on {entry.get('calendar_id') or _UNSPECIFIED} — source event "
        f"{entry.get('source_event_id') or _UNSPECIFIED}, starting "
        f"{_fmt_block_when(start) if start else _UNSPECIFIED}"
    )


def plan_lines(section: dict[str, Any]) -> list[PlanLine]:
    """Lines for the travel-block plan: every block to add, delete or leave alone.

    One line per block to add with its origin, destination, time box and
    minutes, one per block to delete *with the reason it is stale*, one per block
    left alone with the unestablished fact that spared it, then every leg that
    could **not** be priced, phrased so it can never be read as "no commute was
    needed", then a counted summary carrying the Routes call count.

    The delete and left-alone lines are the rehearsal required before any live
    write is allowed: read them on a dry run and confirm nothing outside the plan
    is listed for deletion.
    """
    status = str(section.get("status") or "")
    if status != STATUS_OK:
        return [
            PlanLine(
                logging.INFO,
                f"🚗 travel blocks: {status or _UNSPECIFIED} — no plan computed, no Routes calls",
            )
        ]
    lines: list[PlanLine] = []
    for leg in section.get("adds", []):
        lines.append(
            PlanLine(
                logging.INFO,
                f"   + {leg['person']} {leg['leg']} — “{leg['event']}”: "
                f"{leg['origin']} → {leg['destination']}, "
                f"{_fmt_block_when(leg['start'])}–{_fmt_block_when(leg['end'], time_only=True)} "
                f"({leg['minutes']} min)",
            )
        )
    for pending in section.get("deletes", []):
        lines.append(
            PlanLine(logging.INFO, f"   − delete ({pending['reason']}) {_block_ref(pending)}")
        )
    for spared in section.get("protected", []):
        detail = _PROTECT_DETAIL.get(spared["reason"], _NO_DETAIL)
        lines.append(
            PlanLine(
                logging.INFO,
                f"   ◦ left alone ({spared['reason']}) {_block_ref(spared)}: {detail}, and a "
                f"fact this sweep did not establish is never applied as a delete",
            )
        )
    for failure in section.get("failures", []):
        lines.append(
            PlanLine(
                logging.WARNING,
                f"   ⚠️ unpriced ({failure['reason']}) {failure['person']} {failure['leg']} — "
                f"“{failure['event']}”: {failure['detail']}. No block planned — the drive "
                f"could not be established, this is not a decision that none is needed",
            )
        )
    counts = section.get("counts") or {}
    lines.append(
        PlanLine(
            logging.INFO,
            f"🚗 travel blocks: {counts.get('adds', 0)} add(s), "
            f"{counts.get('deletes', 0)} delete(s), "
            f"{counts.get('keeps', 0)} kept, "
            f"{counts.get('protected', 0)} left alone, "
            f"{counts.get('failures', 0)} unpriced leg(s), "
            f"{section.get('routes_calls', 0)} Routes call(s)"
            + (" [dry-run]" if section.get("dry_run") else ""),
        )
    )
    return lines


def format_travel_blocks_progress(section: dict[str, Any]) -> list[str]:
    """Lines for the travel-block plan and what was applied, for the CLI.

    ``disabled`` is a decision, not an unestablished fact, and it is the state of
    every default install — printing it on every scan would be pure noise. It
    still rides the run payload. The other non-ok statuses are misconfigurations
    of an *enabled* feature and stay loud.
    """
    status = str(section.get("status") or "")
    if not status or status == STATUS_DISABLED:
        return []
    lines = [line.text for line in plan_lines(section)]
    if status == STATUS_OK:
        lines.extend(_apply_lines(section.get("apply") or {}))
    return lines


def _apply_lines(apply: dict[str, Any]) -> list[str]:
    """Lines for what the apply actually did, and every calendar it may not write to.

    A calendar whose capability is ``unknown`` gets its own line: it is neither
    a permission nor a refusal, and folding it into either would hide the one
    state that means "we never found out".
    """
    if not apply:
        return []
    lines: list[str] = []
    for calendar_id, state in sorted((apply.get("write_capability") or {}).items()):
        if state == WRITABLE:
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
