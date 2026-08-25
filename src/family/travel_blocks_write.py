"""The write side of auto-written commute travel blocks (#267, umbrella #263).

This is the only module in the repo that can create or remove a calendar event
for this feature, and it is deliberately the smallest possible surface for it.
:mod:`src.family.travel_blocks` decides *what* should exist and diffs it against
what does; nothing here re-opens that decision.

**Why the shape is what it is.** The write token carries the ``calendar.events``
scope, which can technically delete anything on the calendar. Three structural
choices, not three conventions, keep that from mattering:

1. The reconcile only ever *sees* our own blocks — the listing filters
   server-side on ``privateExtendedProperty=wr_travel_block=1``, so a human's
   event is never fetched, let alone considered.
2. :class:`TravelBlockWriter` owns the raw ``CalendarWriteClient`` privately and
   exposes exactly two operations. :func:`apply_travel_blocks` is handed the
   *writer*, never the client, so ``delete_event`` has no reachable spelling
   other than :meth:`TravelBlockWriter.delete_block` — which cannot run without
   re-verifying the marker on the fetched resource and completing a backup.
3. :meth:`TravelBlockWriter.delete_block` takes an :class:`~src.family.
   travel_blocks.ExistingBlock` (which carries the whole fetched resource), not
   an event id. An id would leave nothing to verify; the resource is what makes
   refusal possible, and refusal is loud — :class:`MarkerGuardError`, never a
   quiet skip. The guard also checks that the ids the API call will *address*
   are the ids of the resource it just verified, so it can never vouch for one
   event while the delete removes another.

Nothing here raises out to :func:`~src.family.calendar_scan.run_calendar_scan`:
a missing or revoked write token, a non-writable calendar, a failed insert and a
failed delete each degrade to a recorded status, following the
``src/analysis/reminders.py`` precedent.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from calendar_readonly.core import CalendarEvent, safe_error_detail
from calendar_write import CalendarWriteClient, build_google_calendar_write_client

from src.config import Config
from src.family.calendar_source import MarkedEvents, fetch_marked_events
from src.family.travel_blocks import (
    MARKER_FILTER,
    MARKER_KEY,
    MARKER_VALUE,
    STATUS_OK,
    ExistingBlock,
    ExistingBlocks,
    PlannedDelete,
    PlannedLeg,
    RouteFn,
    TravelBlockPlan,
    build_block_event,
    carries_marker,
    empty_plan,
    gate_status,
    log_plan,
    parse_existing_block,
    plan_travel_blocks,
    scan_window_days,
    travel_block_horizon,
    travel_block_listing_end,
)
from src.traffic import compute_route

logger = logging.getLogger(__name__)

# --------------------------------------------------------------- write capability

#: The three states a per-calendar write-capability probe can end in. ``UNKNOWN``
#: is not a shade of either other one: an unresolved probe is not permission, and
#: it is not a refusal either. It is skipped like a refusal and *reported* like
#: the unestablished fact it is (global ``CLAUDE.md``).
WRITABLE = "writable"
NOT_WRITABLE = "not_writable"
WRITE_CAPABILITY_UNKNOWN = "unknown"

#: Google's ``accessRole`` values that permit event creation on a calendar.
_WRITING_ROLES = frozenset({"owner", "writer"})
#: ...and the ones that explicitly do not. Anything else is a genuine unknown.
_READING_ROLES = frozenset({"reader", "freeBusyReader"})


def classify_access_role(role: str | None) -> str:
    """Turn a calendar-list ``accessRole`` into a three-state write capability.

    The probe is non-mutating by construction: it reads the role Google already
    publishes rather than inserting a throwaway event and deleting it again —
    which on this feature would mean exercising the delete path to find out
    whether the delete path works.

    The role is the *read* token's view of the calendar, and the write token is
    a second grant on the same Google account, so it is a proxy rather than a
    proof. That is exactly why a failed insert still degrades per-block instead
    of trusting this answer.
    """
    if role in _WRITING_ROLES:
        return WRITABLE
    if role in _READING_ROLES:
        return NOT_WRITABLE
    return WRITE_CAPABILITY_UNKNOWN


# --------------------------------------------------------------- backups

#: Anything outside this set is replaced in a backup filename. Calendar ids are
#: email addresses and event ids are opaque Google strings; neither may be
#: trusted to be path-safe on Windows.
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def default_backup_root() -> Path:
    """``data/calendar_backups`` — under the already-gitignored ``data/`` tree.

    Mirrors :func:`src.family.dedup.default_path`. Backups hold real calendar
    content (titles, addresses), so the path must stay inside ``data/``; do not
    move it, and do not add a second ``.gitignore`` entry for it.
    """
    from src.config import project_root

    return project_root() / "data" / "calendar_backups"


def _safe_component(value: str) -> str:
    cleaned = _UNSAFE_FILENAME.sub("_", value).strip("._")
    return cleaned[:80] or "unknown"


def backup_path(root: Path, *, calendar_id: str, event_id: str, now: datetime) -> Path:
    """``<root>/<YYYY-MM-DD>/<calendar>-<event_id>.json`` — one day per directory."""
    return root / now.strftime("%Y-%m-%d") / (
        f"{_safe_component(calendar_id)}-{_safe_component(event_id)}.json"
    )


def write_backup(
    resource: Mapping[str, Any], *, root: Path, calendar_id: str, event_id: str, now: datetime
) -> Path:
    """Persist the full fetched event resource, returning where it landed.

    Raises on any filesystem failure so the caller *cannot* proceed to the
    delete: an unbacked delete is worse than a stale block, and the only way to
    guarantee that ordering is to make the backup a precondition that throws.
    """
    path = backup_path(root, calendar_id=calendar_id, event_id=event_id, now=now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(resource, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


# --------------------------------------------------------------- the guarded writer


class MarkerGuardError(RuntimeError):
    """A delete was attempted on an event this app did not write.

    Raised, never swallowed at the point of detection: a quiet skip here would
    mean the guard had *stopped* something without anyone finding out it had
    been asked to. Reaching this state is a defect in the reconcile, not a
    Google API failure, and :func:`apply_travel_blocks` logs it at ERROR before
    degrading — the run still completes, but the event is unmistakable.
    """


class BackupFailedError(RuntimeError):
    """The pre-delete backup could not be written, so the delete did not happen.

    Its own type, not a bare ``OSError``, for two reasons. It has to be
    distinguishable from a *delete* that failed — the calendar is in a different
    state in each case — and a raw ``OSError`` cannot be: Google's transport
    errors are ``OSError`` subclasses too, so catching that would misattribute a
    network failure to the disk.
    """


class TravelBlockWriter:
    """The feature's entire calendar-write surface: insert, and guarded delete.

    Holds the ``CalendarWriteClient`` privately and never hands it out. Callers
    receive one of these — never the client — so the only reachable path to
    ``delete_event`` runs through :meth:`delete_block`, and therefore through the
    marker check and the backup. That is what makes deleting someone else's
    event structurally impossible rather than merely discouraged.
    """

    def __init__(
        self, client: CalendarWriteClient, *, backup_root: Path, now: datetime
    ) -> None:
        self._client = client
        self._backup_root = backup_root
        self._now = now
        #: Every backup written this run, in order — the count the payload reports.
        self.backups: list[Path] = []

    def insert_block(self, *, calendar_id: str, event: Mapping[str, Any]) -> str:
        """Create one block and return its new event id (``""`` if Google omits it)."""
        created = self._client.insert_event(calendar_id=calendar_id, event=dict(event))
        return str(created.get("id") or "")

    def delete_block(self, block: ExistingBlock, *, reason: str) -> Path:
        """Back up and then delete one of *our* blocks. Refuses anything else.

        Order is the contract: verify the marker on the resource we actually
        fetched, write the backup, log what is about to go and why, and only
        then call the API. The log line precedes the call deliberately, so a
        crash mid-delete still leaves a record naming the event and its backup.

        The guard is handed the whole ``block``, not just its resource, because
        the resource is what gets *verified* while ``event_id`` / ``calendar_id``
        are what get *addressed* — checking one and calling the other would be a
        guard on a different object from the delete.
        """
        _require_marker(block)
        try:
            path = write_backup(
                block.resource,
                root=self._backup_root,
                calendar_id=block.calendar_id,
                event_id=block.event_id,
                now=self._now,
            )
        except OSError as exc:
            raise BackupFailedError(
                f"no backup written for calendar event {block.event_id!r}, so it was not "
                f"deleted: {exc}"
            ) from exc
        self.backups.append(path)
        logger.info(
            "🗑️ deleting travel block %s on %s (%s: %s leg of source event %s, starts %s) — "
            "backup written to %s",
            block.event_id,
            block.calendar_id,
            reason,
            block.leg or "?",
            block.source_event_id or "?",
            block.start or "?",
            path,
        )
        self._client.delete_event(calendar_id=block.calendar_id, event_id=block.event_id)
        return path

    def close(self) -> None:
        self._client.close()


def _require_marker(block: ExistingBlock) -> None:
    """Raise unless ``block`` is one of ours *and* addresses the resource it verified.

    Two checks, because a guard that validates one object while the caller
    deletes another guards nothing:

    1. the fetched resource carries our marker — this app created it;
    2. the ids the delete will actually send (``event_id``, and ``calendar_id``
       when the resource names one) are the ids *of that same resource*.

    :func:`~src.family.travel_blocks.parse_existing_block` is the only
    constructor in the feature and takes both from one raw resource, so (2)
    cannot fail today. It is asserted anyway: the standard this guard is held to
    is "structurally incapable of deleting an event it did not create", and a
    hand-built record pairing a marked resource with an unrelated ``event_id``
    would otherwise delete that unrelated event. Do not relax it back to a bare
    resource check.
    """
    resource = block.resource
    if not carries_marker(resource):
        raise MarkerGuardError(
            f"refusing to delete calendar event {resource.get('id')!r}: it carries no "
            f"{MARKER_KEY}={MARKER_VALUE} marker, so this app did not create it"
        )
    resource_id = str(resource.get("id") or "")
    if resource_id != block.event_id:
        raise MarkerGuardError(
            f"refusing to delete calendar event {block.event_id!r}: the marked resource that "
            f"was verified is event {resource_id!r}, so the guard and the delete address "
            f"different events"
        )
    resource_calendar = str(resource.get("calendarId") or "")
    if resource_calendar and resource_calendar != block.calendar_id:
        raise MarkerGuardError(
            f"refusing to delete calendar event {block.event_id!r} on {block.calendar_id!r}: "
            f"the marked resource that was verified belongs to calendar "
            f"{resource_calendar!r}"
        )


# --------------------------------------------------------------- apply

#: Why an add or a delete in the plan was not carried out. Each is its own state
#: because "skipped" alone would hide whether we *couldn't* or *wouldn't*.
SKIP_NOT_WRITABLE = "calendar_not_writable"
SKIP_CAPABILITY_UNKNOWN = "write_capability_unknown"
SKIP_STALE_BLOCK_REMAINS = "stale_block_not_removed"
FAILED_INSERT = "insert_failed"
FAILED_DELETE = "delete_failed"
#: Distinct from :data:`FAILED_DELETE` on purpose — the calendar is in a
#: different state in each case (the block is still there either way, but only
#: one of them means the API was ever asked).
FAILED_BACKUP = "backup_failed"
FAILED_MARKER_GUARD = "marker_guard_refused"

APPLY_APPLIED = "applied"
APPLY_DRY_RUN = "dry_run"
APPLY_NO_WRITE_TOKEN = "no_write_token"
APPLY_NOT_PLANNED = "not_planned"


@dataclass(frozen=True)
class ApplyResult:
    """What one apply actually did — counts, per-calendar capability, and every miss.

    ``skipped`` counts planned operations that did not happen, for *any* reason;
    each one also appears in ``failures`` with its own reason, so the number and
    the explanation can never drift apart. ``backups`` is reported because "every
    delete wrote a backup first" is an acceptance criterion, and a criterion you
    cannot read off the run payload is one nobody checks.
    """

    status: str
    inserted: int = 0
    deleted: int = 0
    kept: int = 0
    skipped: int = 0
    backups: int = 0
    write_capability: dict[str, str] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "counts": {
                "inserted": self.inserted,
                "deleted": self.deleted,
                "kept": self.kept,
                "skipped": self.skipped,
                "backups": self.backups,
            },
            "write_capability": dict(self.write_capability),
            "failures": list(self.failures),
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


def apply_travel_blocks(
    plan: TravelBlockPlan,
    *,
    writer: TravelBlockWriter,
    capability: Mapping[str, str],
    title_template: str,
) -> ApplyResult:
    """Carry out ``plan``'s deletes and inserts. Never raises for one event's failure.

    Deletes run first so a replaced block's slot is freed before its successor
    lands — otherwise a sweep that fails between the two leaves two overlapping
    blocks on the calendar instead of none. For the same reason an add whose
    matching delete failed is *skipped*: inserting it would create the duplicate
    the delete was supposed to prevent, and the next sweep would find both.

    ``capability`` gates every calendar. Only :data:`WRITABLE` proceeds — both
    :data:`NOT_WRITABLE` and :data:`WRITE_CAPABILITY_UNKNOWN` are skipped with
    their own recorded reason, because an unresolved probe is not a pass.
    """
    inserted = deleted = skipped = 0
    failures: list[dict[str, Any]] = []
    # Legs whose stale block is still on the calendar; their add must wait.
    blocked_keys: set[str] = set()

    for pending in plan.deletes:
        block = pending.block
        gate = _capability_skip(capability, block.calendar_id)
        if gate is not None:
            skipped += 1
            blocked_keys.add(block.key)
            failures.append(_delete_failure(pending, gate))
            continue
        try:
            writer.delete_block(block, reason=pending.reason)
        except MarkerGuardError as exc:
            # A defect, not an API failure: the reconcile produced a delete for
            # something that is not ours. Loud, and the run still finishes.
            logger.error("❌ travel block delete refused by the marker guard: %s", exc)
            skipped += 1
            blocked_keys.add(block.key)
            failures.append(
                _delete_failure(pending, FAILED_MARKER_GUARD, detail=safe_error_detail(exc))
            )
        except BackupFailedError as exc:
            # An unbacked delete is worse than a stale block, so nothing was
            # deleted. Reported as its own reason: the API was never called.
            logger.warning("⚠️ travel block delete aborted — %s", exc)
            skipped += 1
            blocked_keys.add(block.key)
            failures.append(_delete_failure(pending, FAILED_BACKUP, detail=safe_error_detail(exc)))
        except Exception as exc:  # noqa: BLE001 — one bad delete must not end the sweep
            logger.warning(
                "⚠️ travel block delete failed for %s on %s: %s",
                block.event_id,
                block.calendar_id,
                exc,
            )
            skipped += 1
            blocked_keys.add(block.key)
            failures.append(_delete_failure(pending, FAILED_DELETE, detail=safe_error_detail(exc)))
        else:
            deleted += 1

    for leg in plan.adds:
        gate = _capability_skip(capability, leg.calendar_id)
        if gate is not None:
            skipped += 1
            failures.append(_add_failure(leg, gate))
            continue
        if leg.key in blocked_keys:
            skipped += 1
            failures.append(_add_failure(leg, SKIP_STALE_BLOCK_REMAINS))
            continue
        try:
            event_id = writer.insert_block(
                calendar_id=leg.calendar_id,
                event=build_block_event(leg, title_template=title_template),
            )
        except Exception as exc:  # noqa: BLE001 — one bad insert must not end the sweep
            logger.warning(
                "⚠️ travel block insert failed for %s %s on %s: %s",
                leg.person,
                leg.leg,
                leg.calendar_id,
                exc,
            )
            skipped += 1
            failures.append(_add_failure(leg, FAILED_INSERT, detail=safe_error_detail(exc)))
        else:
            inserted += 1
            logger.info(
                "✅ travel block written %s %s on %s — %s-%s (%d min), event %s",
                leg.person,
                leg.leg,
                leg.calendar_id,
                leg.start.strftime("%a %d %b %H:%M"),
                leg.end.strftime("%H:%M"),
                leg.minutes,
                event_id or "?",
            )

    return ApplyResult(
        status=APPLY_APPLIED,
        inserted=inserted,
        deleted=deleted,
        kept=len(plan.keeps),
        skipped=skipped,
        backups=len(writer.backups),
        write_capability=dict(capability),
        failures=failures,
    )


def _capability_skip(capability: Mapping[str, str], calendar_id: str) -> str | None:
    """The skip reason for a calendar we may not write to, or ``None`` when we may."""
    state = capability.get(calendar_id, WRITE_CAPABILITY_UNKNOWN)
    if state == WRITABLE:
        return None
    return SKIP_NOT_WRITABLE if state == NOT_WRITABLE else SKIP_CAPABILITY_UNKNOWN


def _add_failure(leg: PlannedLeg, reason: str, *, detail: str = "") -> dict[str, Any]:
    return {
        "operation": "insert",
        "reason": reason,
        "detail": detail,
        "person": leg.person,
        "calendar_id": leg.calendar_id,
        "source_event_id": leg.source_event_id,
        "leg": leg.leg,
    }


def _delete_failure(pending: PlannedDelete, reason: str, *, detail: str = "") -> dict[str, Any]:
    block = pending.block
    return {
        "operation": "delete",
        "reason": reason,
        "detail": detail,
        "delete_reason": pending.reason,
        "calendar_id": block.calendar_id,
        "event_id": block.event_id,
        "source_event_id": block.source_event_id,
        "leg": block.leg,
    }


# --------------------------------------------------------------- the sweep


def run_travel_blocks(
    config: Config,
    events_by_person: Mapping[str, Sequence[CalendarEvent]],
    *,
    now: datetime,
    session: requests.Session | None = None,
    route_fn: RouteFn = compute_route,
    backup_root: Path | None = None,
    force_dry_run: bool = False,
) -> dict[str, Any]:
    """Plan, reconcile and (unless dry-run) apply the horizon's travel blocks.

    The one entry point :func:`~src.family.calendar_scan.run_calendar_scan`
    calls, and the only place the three phases are ordered. Returns the run
    payload's ``travel_blocks`` section; every pre-existing key of it keeps its
    meaning, with ``apply`` added.

    The gate is checked *first* so a disabled feature — the state of every
    default install — spends no Calendar read at all. ``dry_run`` (the shipped
    default) short-circuits before a write client is ever built: not "build one
    and don't call it", but no token load, no client, no writer, and therefore
    no object in the process capable of an insert, a delete or a backup.

    ``force_dry_run`` tightens the configured mode for this one call and can
    only ever tighten it — ``settings.dry_run or force_dry_run``, never the
    other way round, so no caller can talk a dry-run install into writing. It
    exists because ``calendar-scan --dry-run`` used to suppress only the
    *summary alert*: the sweep riding along inside it still wrote to real
    calendars whenever ``travel_blocks.dry_run`` was off, which made "dry run"
    a lie for the one part of the verb that mutates anything outside this
    process. The Family tab's rehearse control (#276) depends on that being
    server-enforced, not merely a disabled button.
    """
    settings = config.family.travel_blocks
    dry_run = settings.dry_run or force_dry_run
    gate = gate_status(config)
    if gate is not None:
        plan = empty_plan(gate, dry_run)
        log_plan(plan)
        return plan.to_payload()

    horizon_start, horizon_end = travel_block_horizon(config, now)
    _warn_if_horizon_clamped(config)
    # The read is padded past the horizon, the plan is not (#272). See
    # `travel_block_listing_end` for the bound and `reconcile` for why a block
    # found out there is left alone rather than judged an orphan.
    marked = _read_marked(
        config,
        time_min=horizon_start,
        time_max=travel_block_listing_end(
            events_by_person, horizon_start=horizon_start, horizon_end=horizon_end
        ),
    )
    plan = plan_travel_blocks(
        config,
        events_by_person,
        now=now,
        existing=_existing_blocks(marked),
        session=session,
        route_fn=route_fn,
        dry_run=dry_run,
    )
    capability = {
        calendar_id: classify_access_role(role) for calendar_id, role in marked.access_roles.items()
    }
    result = _apply(
        config, plan, capability=capability, now=now, backup_root=backup_root, dry_run=dry_run
    )
    _log_apply(result)

    payload = plan.to_payload()
    if plan.status == STATUS_OK:
        payload["apply"] = result.to_payload()
    return payload


def _warn_if_horizon_clamped(config: Config) -> None:
    """Say so when ``horizon_days`` is wider than the events the scan actually fetches.

    Silently clamping a configured number is how a knob comes to mean something
    other than what it says. It is clamped (a wider horizon would orphan-delete
    every block past the fetched days on every run), and the operator is told.
    """
    family = config.family
    configured = family.travel_blocks.horizon_days or family.assessment_days
    available = scan_window_days(config)
    if configured > available:
        logger.warning(
            "⚠️ travel blocks: horizon_days=%d exceeds the %d day(s) of events the daily scan "
            "fetches; using %d. Raise family.unknown_scan_days to widen it for real",
            configured,
            available,
            available,
        )


def _read_marked(config: Config, *, time_min: datetime, time_max: datetime) -> MarkedEvents:
    """List our own blocks over the padded window; a total read failure is still a *known* unknown.

    If the read client itself cannot be built, every configured calendar is
    reported unreadable rather than empty — an empty listing would be read as
    "no blocks exist" and re-insert the entire horizon on the next live run.
    """
    try:
        return fetch_marked_events(
            config.calendar,
            time_min=time_min,
            time_max=time_max,
            private_extended_property=MARKER_FILTER,
        )
    except Exception as exc:  # noqa: BLE001 — degrade to "unknown", never to "empty"
        logger.warning("⚠️ travel blocks: could not read existing blocks: %s", exc)
        detail = f"calendar read failed ({type(exc).__name__})"
        calendar_ids = [account.calendar_id for account in config.calendar.accounts]
        return MarkedEvents(
            marked={},
            unreadable=dict.fromkeys(calendar_ids, detail),
            access_roles=dict.fromkeys(calendar_ids, None),
        )


def _existing_blocks(marked: MarkedEvents) -> ExistingBlocks:
    """Parse the fetched resources into the records the reconcile and delete accept."""
    blocks: list[ExistingBlock] = []
    for calendar_id, resources in marked.marked.items():
        for raw in resources:
            block = parse_existing_block(raw, calendar_id=calendar_id)
            if block is not None:
                blocks.append(block)
    return ExistingBlocks(blocks=tuple(blocks), unreadable=dict(marked.unreadable))


def _apply(
    config: Config,
    plan: TravelBlockPlan,
    *,
    capability: Mapping[str, str],
    now: datetime,
    backup_root: Path | None,
    dry_run: bool,
) -> ApplyResult:
    """Build the writer and apply, or report exactly why nothing was written.

    ``dry_run`` is passed in rather than re-read off the config so this decision
    and the plan's own ``dry_run`` flag can never disagree: one value, decided
    once in :func:`run_travel_blocks`, drives the short-circuit *and* what the
    payload claims happened.
    """
    settings = config.family.travel_blocks
    if plan.status != STATUS_OK:
        return ApplyResult(status=APPLY_NOT_PLANNED, write_capability=dict(capability))
    planned = len(plan.adds) + len(plan.deletes)
    if dry_run:
        return ApplyResult(
            status=APPLY_DRY_RUN,
            kept=len(plan.keeps),
            skipped=planned,
            write_capability=dict(capability),
        )
    try:
        client = build_google_calendar_write_client(config.calendar.write_token_path)
    except Exception as exc:  # noqa: BLE001 — a missing/revoked token is a status, not a crash
        logger.warning("⚠️ travel blocks: no usable calendar write token — nothing written: %s", exc)
        return ApplyResult(
            status=APPLY_NO_WRITE_TOKEN,
            kept=len(plan.keeps),
            skipped=planned,
            write_capability=dict(capability),
            detail=safe_error_detail(exc),
        )
    writer = TravelBlockWriter(client, backup_root=backup_root or default_backup_root(), now=now)
    try:
        return apply_travel_blocks(
            plan,
            writer=writer,
            capability=capability,
            title_template=settings.title_template,
        )
    finally:
        try:
            writer.close()
        except Exception as exc:  # noqa: BLE001 — closing a client must not fail a run
            logger.debug("closing the calendar write client failed: %s", exc)


def _log_apply(result: ApplyResult, *, log: logging.Logger | None = None) -> None:
    """One counted apply line, plus every calendar we may not (or may not know we may) write."""
    out = log or logger
    if result.status == APPLY_NOT_PLANNED:
        return
    for calendar_id, state in sorted(result.write_capability.items()):
        if state == WRITABLE:
            continue
        out.warning(
            "⚠️ travel blocks: calendar %s is %s — no blocks written to it%s",
            calendar_id,
            state,
            (
                ". 'unknown' is not 'not writable': the capability could not be "
                "established, so nothing was attempted"
                if state == WRITE_CAPABILITY_UNKNOWN
                else ""
            ),
        )
    out.info(
        "%s travel blocks [%s]: %d inserted, %d deleted, %d kept, %d skipped, %d backup(s)%s",
        "⚠️" if result.failures else "✅",
        result.status,
        result.inserted,
        result.deleted,
        result.kept,
        result.skipped,
        result.backups,
        f" — {result.detail}" if result.detail else "",
    )
