"""Reconcile + guarded apply for auto-written travel blocks (#267, umbrella #263).

Offline and deterministic: no Google client, no Routes call, no network, no
token. Both calendar clients are fakes, the Routes function is a stub, and every
backup lands in a pytest ``tmp_path`` — nothing here can reach a real calendar.
All fixture people, addresses and calendar ids are invented.

The delete path is what these tests exist for. It is exercised from three
angles: behaviour (a marked block is deleted, an unmarked resource is refused
loudly), ordering (the backup file is on disk *before* the API call, and a
failed backup aborts the delete), and structure (there is no reachable spelling
of ``delete_event`` that skips the guard).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.config import (
    CalendarAccount,
    CalendarConfig,
    Config,
    FamilyConfig,
    HubConfig,
    TelegramConfig,
    TrafficConfig,
    TravelBlocksConfig,
)
from src.config.calendar import parse as parse_calendar_accounts
from src.family import calendar_source, travel_blocks, travel_blocks_write
from src.family.travel_blocks import (
    LEG_OUTBOUND,
    LEG_RETURN,
    MARKER_FILTER,
    ExistingBlock,
    PlannedLeg,
    reconcile,
)
from src.family.travel_blocks_write import (
    MarkerGuardError,
    TravelBlockWriter,
    apply_travel_blocks,
    classify_access_role,
)
from src.traffic import RouteResult, TrafficReadError

HOME = "1 Example Street, Sample Town"
OFFICE = "3 Example Road, Sample City"
CLINIC = "4 Example Lane, Sample Town"

PERSON = "parent-a"
CALENDAR_ID = "parent-a@example.test"
PERSON_B = "parent-b"
CALENDAR_B = "parent-b@example.test"

DAY = datetime(2026, 7, 20, tzinfo=UTC)
NOW = DAY.replace(hour=6)
TITLE = "🚗 Trayecto"
#: A plan window comfortably containing every fixture leg below, for the *pure*
#: reconcile tests. The listing deliberately reads past where the plan stops
#: (#272), so the diff has to be told which of the two moments it is judging
#: against — a block beyond it is unplanned-for, not orphaned. The sweep tests
#: derive the real boundary from `_horizon()` instead: the true horizon depends
#: on the machine's local midnight, which is not `DAY`'s.
PLAN_END = DAY + timedelta(days=2)


def _at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


# --------------------------------------------------------------- fixtures & doubles


def _raw_event(
    eid: str,
    *,
    location: str = OFFICE,
    summary: str = "Appointment",
    start: datetime | None = None,
    hours: int = 1,
) -> dict[str, Any]:
    """A human's calendar event, as the scan's own fetch returns it."""
    start = start or _at(9)
    return {
        "id": eid,
        "summary": summary,
        "location": location,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=hours)).isoformat()},
    }


def _leg(
    *,
    leg: str = LEG_OUTBOUND,
    source_event_id: str = "e1",
    calendar_id: str = CALENDAR_ID,
    person: str = PERSON,
    origin: str = HOME,
    destination: str = OFFICE,
    minutes: int = 20,
    start: datetime | None = None,
    end: datetime | None = None,
) -> PlannedLeg:
    return PlannedLeg(
        person=person,
        calendar_id=calendar_id,
        leg=leg,
        source_event_id=source_event_id,
        origin=origin,
        destination=destination,
        start=start or _at(8, 40),
        end=end or _at(9),
        source_start=_at(9),
        source_end=_at(10),
        minutes=minutes,
    )


def _raw_block(leg: PlannedLeg, *, event_id: str = "blk-1") -> dict[str, Any]:
    """The resource this app writes for ``leg`` — marker and all — read back."""
    return {
        "id": event_id,
        "summary": TITLE,
        "location": leg.destination,
        "start": {"dateTime": leg.start.isoformat()},
        "end": {"dateTime": leg.end.isoformat()},
        "extendedProperties": {"private": travel_blocks.block_marker(leg)},
    }


def _block(leg: PlannedLeg, *, event_id: str = "blk-1") -> ExistingBlock:
    parsed = travel_blocks.parse_existing_block(
        _raw_block(leg, event_id=event_id), calendar_id=leg.calendar_id
    )
    assert parsed is not None
    return parsed


def _within_window(
    event: dict[str, Any], *, time_min: datetime, time_max: datetime
) -> bool:
    """Google's own `events.list` window semantics, which #272 turns on.

    ``timeMin`` is an exclusive lower bound on an event's **end**; ``timeMax`` an
    exclusive upper bound on its **start**. The asymmetry is the whole bug: a
    block starting at or after ``timeMax`` is simply not returned, so the
    reconcile could not see the block it had written and re-inserted it every
    sweep. A fake that ignored the window could not reproduce that.
    """
    start = datetime.fromisoformat(event["start"]["dateTime"])
    end = datetime.fromisoformat(event["end"]["dateTime"])
    return end > time_min and start < time_max


class _FakeReadClient:
    """Stands in for the Calendar read client; records how it was queried."""

    def __init__(
        self,
        marked: dict[str, list[dict[str, Any]]] | None = None,
        *,
        roles: dict[str, str | None] | None = None,
        list_fails: Sequence[str] = (),
        role_fails: Sequence[str] = (),
    ) -> None:
        self._marked = marked or {}
        self._roles = roles if roles is not None else {}
        self._list_fails = set(list_fails)
        self._role_fails = set(role_fails)
        self.list_kwargs: list[dict[str, Any]] = []
        self.closed = False

    def list_events(
        self,
        *,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        private_extended_property: str | None = None,
    ) -> list[dict[str, Any]]:
        self.list_kwargs.append({
            "calendar_id": calendar_id,
            "time_min": time_min,
            "time_max": time_max,
            "private_extended_property": private_extended_property,
        })
        if calendar_id in self._list_fails:
            raise RuntimeError("calendar listing exploded")
        return [
            event
            for event in self._marked.get(calendar_id, [])
            if _within_window(event, time_min=time_min, time_max=time_max)
        ]

    def calendar_access_role(self, calendar_id: str) -> str | None:
        if calendar_id in self._role_fails:
            raise RuntimeError("calendarList unavailable")
        return self._roles.get(calendar_id, "owner")

    def close(self) -> None:
        self.closed = True


class _FakeWriteClient:
    """Records inserts and deletes; never talks to Google."""

    def __init__(
        self, *, insert_fails: Sequence[str] = (), delete_fails: Sequence[str] = ()
    ) -> None:
        self.inserted: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []
        #: Whether a backup for the event already existed at delete time — the
        #: only honest way to assert the *ordering* of backup and API call.
        self.backup_present_at_delete: list[bool] = []
        self.backup_root: Path | None = None
        self.closed = False
        self._insert_fails = set(insert_fails)
        self._delete_fails = set(delete_fails)

    def insert_event(self, *, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
        if calendar_id in self._insert_fails:
            raise RuntimeError("insert rejected")
        self.inserted.append((calendar_id, event))
        return {"id": f"new-{len(self.inserted)}"}

    def delete_event(self, *, calendar_id: str, event_id: str) -> None:
        if self.backup_root is not None:
            self.backup_present_at_delete.append(
                travel_blocks_write.backup_path(
                    self.backup_root, calendar_id=calendar_id, event_id=event_id, now=NOW
                ).is_file()
            )
        if event_id in self._delete_fails:
            raise RuntimeError("delete rejected")
        self.deleted.append((calendar_id, event_id))

    def close(self) -> None:
        self.closed = True


class _StubRoutes:
    """A `RouteFn` that answers a fixed duration, or raises like the real client.

    ``fails`` reproduces a Routes outage — a transport error, a non-200 (an
    ``HTTP 429`` rate-limit included) or an empty ``routes`` list all surface as
    :class:`TrafficReadError`.
    """

    def __init__(self, minutes: float = 20, *, fails: bool = False) -> None:
        self._minutes = minutes
        self._fails = fails
        self.calls = 0

    def __call__(
        self,
        origin: str,
        destination: str,
        *,
        api_key: str,
        departure_time: datetime | None = None,
        session: Any = None,
    ) -> RouteResult:
        self.calls += 1
        if self._fails:
            raise TrafficReadError("Routes API returned HTTP 429")
        return RouteResult(normal_s=int(self._minutes * 60), traffic_s=int(self._minutes * 60))


def _config(
    *,
    enabled: bool = True,
    dry_run: bool = False,
    accounts: tuple[CalendarAccount, ...] = (
        CalendarAccount(calendar_id=CALENDAR_ID, person=PERSON),
    ),
) -> Config:
    return Config(
        db_path="unused.sqlite3",  # type: ignore[arg-type]
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="m"),
        notifier="telegram",
        telegram=TelegramConfig(bot_token="t", chat_id="c"),
        linked_device_dir="ld",  # type: ignore[arg-type]
        calendar=CalendarConfig(accounts=accounts),
        traffic=TrafficConfig(api_key="routes-key", origin_lookback_min=60),
        family=FamilyConfig(
            enabled=True,
            home_address=HOME,
            travel_blocks=TravelBlocksConfig(
                enabled=enabled, dry_run=dry_run, horizon_days=2, title_template=TITLE
            ),
        ),
    )


def _horizon() -> tuple[datetime, datetime]:
    """The sweep's real planning window for :data:`NOW`.

    Computed, never hardcoded: it starts at the *machine's* local midnight, so a
    literal would only be right in one timezone.
    """
    return travel_blocks.travel_block_horizon(_config(), NOW)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    events: dict[str, list[Any]] | None = None,
    read: _FakeReadClient | None = None,
    write: _FakeWriteClient | None = None,
    write_client_error: Exception | None = None,
    route_fn: _StubRoutes | None = None,
    now: datetime = NOW,
    **config_kwargs: Any,
) -> tuple[dict[str, Any], _FakeReadClient, _FakeWriteClient]:
    """Drive `run_travel_blocks` end to end with both clients faked."""
    from calendar_readonly.core import normalize_event

    read_client = read or _FakeReadClient()
    write_client = write or _FakeWriteClient()
    write_client.backup_root = tmp_path

    def build_write(_path: Path) -> _FakeWriteClient:
        if write_client_error is not None:
            raise write_client_error
        return write_client

    monkeypatch.setattr(calendar_source, "build_google_calendar_client", lambda _p: read_client)
    monkeypatch.setattr(travel_blocks_write, "build_google_calendar_write_client", build_write)

    raw = events if events is not None else {PERSON: [_raw_event("e1")]}
    normalized = {
        person: [normalize_event(item, calendar_id=CALENDAR_ID) for item in items]
        for person, items in raw.items()
    }
    payload = travel_blocks_write.run_travel_blocks(
        _config(**config_kwargs),
        normalized,
        now=now,
        route_fn=route_fn or _StubRoutes(),
        backup_root=tmp_path,
    )
    return payload, read_client, write_client


# --------------------------------------------------------------- the reconcile (pure)


def test_a_missing_block_is_an_add() -> None:
    leg = _leg()
    diff = reconcile([leg], [], plan_end=PLAN_END)
    assert diff.adds == [leg]
    assert diff.deletes == [] and diff.keeps == []


def test_an_unchanged_block_is_kept_and_never_rewritten() -> None:
    """The zero-write contract, at its source: same hash, same schema, no ops."""
    leg = _leg()
    diff = reconcile([leg], [_block(leg)], plan_end=PLAN_END)
    assert diff.adds == [] and diff.deletes == []
    assert [block.event_id for block in diff.keeps] == ["blk-1"]


def test_a_changed_hash_is_a_delete_and_a_reinsert() -> None:
    """A moved source event, or a new location, invalidates the stored hash."""
    stale = _block(_leg(minutes=20))
    desired = _leg(minutes=35)  # a different drive length => a different hash
    diff = reconcile([desired], [stale], plan_end=PLAN_END)
    assert diff.adds == [desired]
    assert [(d.block.event_id, d.reason) for d in diff.deletes] == [
        ("blk-1", travel_blocks.DELETE_REASON_REPLACED)
    ]
    assert diff.keeps == []


def test_a_vanished_source_event_leaves_an_orphan_to_delete() -> None:
    """Cancelled, or simply out of the horizon: nothing desires this block now."""
    diff = reconcile([], [_block(_leg())], plan_end=PLAN_END)
    assert diff.adds == [] and diff.keeps == []
    assert [d.reason for d in diff.deletes] == [travel_blocks.DELETE_REASON_ORPHANED]


def test_an_unrecognised_schema_version_is_never_a_keep() -> None:
    """A build that cannot vouch for a block's shape must not certify it."""
    leg = _leg()
    raw = _raw_block(leg)
    raw["extendedProperties"]["private"]["wr_schema_version"] = "99"
    stale = travel_blocks.parse_existing_block(raw, calendar_id=CALENDAR_ID)
    assert stale is not None
    diff = reconcile([leg], [stale], plan_end=PLAN_END)
    assert diff.adds == [leg]
    assert [d.reason for d in diff.deletes] == [travel_blocks.DELETE_REASON_REPLACED]


def test_a_duplicate_block_for_one_leg_is_removed_not_tolerated() -> None:
    leg = _leg()
    diff = reconcile(
        [leg], [_block(leg, event_id="blk-1"), _block(leg, event_id="blk-2")], plan_end=PLAN_END
    )
    assert diff.adds == []
    assert [block.event_id for block in diff.keeps] == ["blk-1"]
    assert [(d.block.event_id, d.reason) for d in diff.deletes] == [
        ("blk-2", travel_blocks.DELETE_REASON_DUPLICATE)
    ]


def test_a_protected_leg_is_neither_deleted_nor_re_added() -> None:
    """A leg we could not plan is *unknown*, not *unwanted* — so its block is untouched.

    Without the protected set the leg is simply absent from `desired`, its block
    matches nothing, and the orphan sweep deletes it.
    """
    leg = _leg()
    block = _block(leg)

    diff = reconcile([], [block], plan_end=PLAN_END, protected={leg.key})

    assert diff.deletes == [] and diff.adds == [] and diff.keeps == []
    # Left alone, and said out loud — with *which* unestablished fact spared it.
    assert [(p.block, p.reason) for p in diff.protected] == [
        (block, travel_blocks.PROTECT_REASON_UNPLANNED_LEG)
    ]


def test_protection_covers_only_the_leg_that_failed() -> None:
    """One unplannable leg must not shield a genuinely orphaned block on the same calendar."""
    failed = _leg(source_event_id="e1")
    orphan = _block(_leg(source_event_id="e2"), event_id="blk-2")

    diff = reconcile(
        [], [_block(failed), orphan], plan_end=PLAN_END, protected={failed.key}
    )

    assert [pending.block for pending in diff.deletes] == [orphan]
    assert [pending.reason for pending in diff.deletes] == [
        travel_blocks.DELETE_REASON_ORPHANED
    ]
    assert [pending.block for pending in diff.protected] == [_block(failed)]


def test_an_orphan_past_the_plan_window_is_left_alone_not_deleted() -> None:
    """#272's trap: the padded read must widen what we *see*, never what we judge.

    A block starting past ``plan_end`` was only fetched because the listing now
    reads further than the plan does. Nothing computed a desired counterpart out
    there, so "no counterpart" is not evidence of an orphan — and treating it as
    one would turn a fix for duplicate blocks into a delete of correct ones.
    """
    beyond = _block(
        _leg(source_event_id="e-late", start=PLAN_END + timedelta(hours=1),
             end=PLAN_END + timedelta(hours=1, minutes=20)),
        event_id="blk-late",
    )

    diff = reconcile([], [beyond], plan_end=PLAN_END)

    assert diff.deletes == [] and diff.adds == [] and diff.keeps == []
    assert [(p.block.event_id, p.reason) for p in diff.protected] == [
        ("blk-late", travel_blocks.PROTECT_REASON_BEYOND_HORIZON)
    ]


def test_a_block_starting_exactly_at_the_plan_end_is_outside_it() -> None:
    """``plan_end`` is exclusive: a block starting exactly there is already outside it.

    The same half-open convention `desired_legs` uses for events
    (``horizon_start <= start < horizon_end``), so the two edges agree.
    """
    edge = _block(
        _leg(source_event_id="e-edge", start=PLAN_END, end=PLAN_END + timedelta(minutes=20)),
        event_id="blk-edge",
    )
    inside = _block(
        _leg(source_event_id="e-in", start=PLAN_END - timedelta(minutes=1),
             end=PLAN_END + timedelta(minutes=19)),
        event_id="blk-in",
    )

    diff = reconcile([], [edge, inside], plan_end=PLAN_END)

    assert [p.block.event_id for p in diff.protected] == ["blk-edge"]
    assert [(d.block.event_id, d.reason) for d in diff.deletes] == [
        ("blk-in", travel_blocks.DELETE_REASON_ORPHANED)
    ]


def test_a_block_whose_start_cannot_be_read_is_never_deleted() -> None:
    """Not placeable is not "inside" — an unestablished position is its own state."""
    raw = _raw_block(_leg())
    raw["start"] = {"date": "2026-07-20"}  # all-day: no moment to compare at all
    block = travel_blocks.parse_existing_block(raw, calendar_id=CALENDAR_ID)
    assert block is not None

    diff = reconcile([], [block], plan_end=PLAN_END)

    assert diff.deletes == []
    assert [p.reason for p in diff.protected] == [travel_blocks.PROTECT_REASON_START_UNKNOWN]


def test_parse_refuses_a_resource_without_the_marker() -> None:
    """The only constructor the delete path accepts is itself marker-checking."""
    assert travel_blocks.parse_existing_block(_raw_event("e1"), calendar_id=CALENDAR_ID) is None


# --------------------------------------------------------------- the block resource


def test_the_written_block_is_private_busy_and_silent() -> None:
    event = travel_blocks.build_block_event(_leg(destination=CLINIC), title_template=TITLE)
    assert event["summary"] == TITLE
    assert CLINIC not in event["summary"]  # a shared view must leak no destination
    assert event["location"] == CLINIC  # ...but tapping it must open navigation
    assert event["transparency"] == "opaque"
    assert event["visibility"] == "private"
    assert event["reminders"] == {"useDefault": False, "overrides": []}
    assert event["extendedProperties"]["private"][travel_blocks.MARKER_KEY] == "1"


# --------------------------------------------------------------- the delete guard


def test_deleting_an_unmarked_resource_raises_loudly(tmp_path: Path) -> None:
    """Refusal is an exception, never a quiet skip — and never reaches the API."""
    client = _FakeWriteClient()
    writer = TravelBlockWriter(client, backup_root=tmp_path, now=NOW)
    impostor = ExistingBlock(
        calendar_id=CALENDAR_ID,
        event_id="human-event",
        source_event_id="e1",
        leg=LEG_OUTBOUND,
        schema_version="1",
        stored_hash="whatever",
        resource=_raw_event("human-event"),  # a real human event: no marker
    )
    with pytest.raises(MarkerGuardError) as caught:
        writer.delete_block(impostor, reason="orphaned")
    assert travel_blocks.MARKER_KEY in str(caught.value)
    assert client.deleted == []
    assert list(tmp_path.rglob("*.json")) == []  # not even a backup was written


def test_a_wrong_marker_value_is_refused_too(tmp_path: Path) -> None:
    """`wr_travel_block: "0"` is not ours either — the check is on the value."""
    raw = _raw_event("x")
    raw["extendedProperties"] = {"private": {travel_blocks.MARKER_KEY: "0"}}
    block = ExistingBlock(
        calendar_id=CALENDAR_ID,
        event_id="x",
        source_event_id="e1",
        leg=LEG_OUTBOUND,
        schema_version="1",
        stored_hash="h",
        resource=raw,
    )
    writer = TravelBlockWriter(_FakeWriteClient(), backup_root=tmp_path, now=NOW)
    with pytest.raises(MarkerGuardError):
        writer.delete_block(block, reason="orphaned")


def test_a_block_addressing_a_different_event_than_it_verifies_is_refused(
    tmp_path: Path,
) -> None:
    """The guard must validate the same event the API call addresses, not another one.

    `parse_existing_block` takes both from one raw resource, so this cannot
    happen today — which is exactly why it is pinned: a hand-built record
    pairing *our* marked resource with someone else's ``event_id`` would
    otherwise sail through the marker check and delete that other event.
    """
    client = _FakeWriteClient()
    writer = TravelBlockWriter(client, backup_root=tmp_path, now=NOW)
    marked = _block(_leg(), event_id="blk-1")
    mismatched = ExistingBlock(
        calendar_id=marked.calendar_id,
        event_id="someone-elses-event",  # not the id of the resource below
        source_event_id=marked.source_event_id,
        leg=marked.leg,
        schema_version=marked.schema_version,
        stored_hash=marked.stored_hash,
        resource=marked.resource,
    )

    with pytest.raises(MarkerGuardError) as caught:
        writer.delete_block(mismatched, reason=travel_blocks.DELETE_REASON_ORPHANED)

    assert "someone-elses-event" in str(caught.value)
    assert client.deleted == []
    assert list(tmp_path.rglob("*.json")) == []  # refused before any backup


def test_a_block_addressing_a_different_calendar_than_it_verifies_is_refused(
    tmp_path: Path,
) -> None:
    """Same rule for the calendar id, when the fetched resource names one."""
    client = _FakeWriteClient()
    writer = TravelBlockWriter(client, backup_root=tmp_path, now=NOW)
    marked = _block(_leg())
    resource = {**marked.resource, "calendarId": CALENDAR_B}
    mismatched = ExistingBlock(
        calendar_id=CALENDAR_ID,
        event_id=marked.event_id,
        source_event_id=marked.source_event_id,
        leg=marked.leg,
        schema_version=marked.schema_version,
        stored_hash=marked.stored_hash,
        resource=resource,
    )

    with pytest.raises(MarkerGuardError):
        writer.delete_block(mismatched, reason=travel_blocks.DELETE_REASON_ORPHANED)
    assert client.deleted == []


def test_the_backup_is_on_disk_before_the_delete_call(tmp_path: Path) -> None:
    """Ordering, asserted from inside the API call itself."""
    client = _FakeWriteClient()
    client.backup_root = tmp_path
    writer = TravelBlockWriter(client, backup_root=tmp_path, now=NOW)
    block = _block(_leg())

    path = writer.delete_block(block, reason=travel_blocks.DELETE_REASON_ORPHANED)

    assert client.backup_present_at_delete == [True]
    assert client.deleted == [(CALENDAR_ID, "blk-1")]
    assert path.parent.name == "2026-07-20"
    assert json.loads(path.read_text(encoding="utf-8")) == block.resource
    assert writer.backups == [path]


def test_the_backup_filename_is_sanitised(tmp_path: Path) -> None:
    """Calendar ids are email addresses; event ids are opaque. Neither is path-safe."""
    path = travel_blocks_write.backup_path(
        tmp_path, calendar_id="a/b:c@example.test", event_id="../evil id", now=NOW
    )
    assert path.parent.parent == tmp_path
    for forbidden in ("/", "\\", ":", "..", " "):
        assert forbidden not in path.name


def test_a_failed_backup_aborts_the_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unbacked delete is worse than a stale block, so the backup throws."""
    client = _FakeWriteClient()
    writer = TravelBlockWriter(client, backup_root=tmp_path, now=NOW)

    def explode(*_args: object, **_kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(travel_blocks_write, "write_backup", explode)
    block = _block(_leg())
    with pytest.raises(travel_blocks_write.BackupFailedError, match="disk full"):
        writer.delete_block(block, reason="orphaned")
    assert client.deleted == []
    assert writer.backups == []

    # ...and `apply` records it under its own reason, distinct from a delete
    # that was actually attempted and rejected by Google.
    plan = travel_blocks.TravelBlockPlan(
        status=travel_blocks.STATUS_OK,
        dry_run=False,
        legs=[],
        adds=[],
        deletes=[travel_blocks.PlannedDelete(block, travel_blocks.DELETE_REASON_ORPHANED)],
        failures=[],
        routes_calls=0,
    )
    result = apply_travel_blocks(
        plan,
        writer=writer,
        capability={CALENDAR_ID: travel_blocks_write.WRITABLE},
        title_template=TITLE,
    )
    assert client.deleted == []
    assert result.deleted == 0 and result.backups == 0
    assert [f["reason"] for f in result.failures] == [travel_blocks_write.FAILED_BACKUP]
    assert travel_blocks_write.FAILED_BACKUP != travel_blocks_write.FAILED_DELETE


def test_the_portable_calendar_packages_stay_product_neutral() -> None:
    """`calendar_readonly` / `calendar_write` must remain liftable into any repo.

    They transport the ``privateExtendedProperty`` filter without knowing what a
    marker *is* — the ``wr_*`` vocabulary, the block title and the backup path all
    live in ``src/family``. Nothing here may reach back into this application.
    """
    root = Path(__file__).resolve().parents[1]
    product_specific = ("wr_travel_block", "wr_source_event_id", "Trayecto", "calendar_backups")
    for package in ("calendar_readonly", "calendar_write"):
        for path in (root / package).glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("from src", "import src", "from app", "import app", "from scripts"):
                assert forbidden not in source, f"{path.name} must not import {forbidden}"
            for term in product_specific:
                assert term not in source, f"{path.name} must not name {term}"


def test_delete_event_is_reachable_only_through_the_guarded_writer() -> None:
    """Structural, not behavioural: no stub can prove the absence of a code path.

    Exactly one call site in the whole feature, inside `delete_block`, and the
    marker check and the backup both precede it there. Reordering them, or
    adding a second call site, fails this test rather than a code review.
    """
    root = Path(__file__).resolve().parents[1]
    family = root / "src" / "family"
    call_sites = {
        path.name: path.read_text(encoding="utf-8").count("delete_event(")
        for path in family.glob("*.py")
    }
    assert sum(call_sites.values()) == 1, call_sites
    assert call_sites["travel_blocks_write.py"] == 1

    source = (family / "travel_blocks_write.py").read_text(encoding="utf-8")
    body = source.split("def delete_block(", 1)[1].split("\n    def ", 1)[0]
    guard = body.index("_require_marker(")
    backup = body.index("write_backup(")
    call = body.index("self._client.delete_event(")
    assert guard < backup < call, "the guard and the backup must both precede the API call"


# --------------------------------------------------------------- write capability


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("owner", travel_blocks_write.WRITABLE),
        ("writer", travel_blocks_write.WRITABLE),
        ("reader", travel_blocks_write.NOT_WRITABLE),
        ("freeBusyReader", travel_blocks_write.NOT_WRITABLE),
        (None, travel_blocks_write.WRITE_CAPABILITY_UNKNOWN),
        ("something-new", travel_blocks_write.WRITE_CAPABILITY_UNKNOWN),
    ],
)
def test_the_capability_probe_has_three_states(role: str | None, expected: str) -> None:
    """`unknown` is its own answer — never folded into writable or not_writable."""
    assert classify_access_role(role) == expected
    assert (
        travel_blocks_write.WRITE_CAPABILITY_UNKNOWN
        not in (travel_blocks_write.WRITABLE, travel_blocks_write.NOT_WRITABLE)
    )


def test_a_read_only_calendar_is_skipped_with_a_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload, _, write = _run(
        monkeypatch, tmp_path, read=_FakeReadClient(roles={CALENDAR_ID: "reader"})
    )
    apply = payload["apply"]
    assert write.inserted == []
    assert apply["write_capability"] == {CALENDAR_ID: travel_blocks_write.NOT_WRITABLE}
    assert apply["counts"]["inserted"] == 0
    assert apply["counts"]["skipped"] == len(payload["adds"]) > 0
    assert {f["reason"] for f in apply["failures"]} == {travel_blocks_write.SKIP_NOT_WRITABLE}


def test_an_unresolved_capability_probe_skips_and_says_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A probe that failed is not permission — and is reported as neither."""
    payload, _, write = _run(
        monkeypatch, tmp_path, read=_FakeReadClient(role_fails=[CALENDAR_ID])
    )
    apply = payload["apply"]
    assert write.inserted == []
    assert apply["write_capability"] == {CALENDAR_ID: travel_blocks_write.WRITE_CAPABILITY_UNKNOWN}
    assert {f["reason"] for f in apply["failures"]} == {
        travel_blocks_write.SKIP_CAPABILITY_UNKNOWN
    }


# --------------------------------------------------------------- the live sweep


def test_a_live_sweep_writes_an_outbound_and_a_return_on_the_persons_own_calendar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload, read, write = _run(monkeypatch, tmp_path)

    assert [calendar_id for calendar_id, _ in write.inserted] == [CALENDAR_ID, CALENDAR_ID]
    legs = [
        event["extendedProperties"]["private"][travel_blocks.LEG_KEY]
        for _, event in write.inserted
    ]
    assert set(legs) == {LEG_OUTBOUND, LEG_RETURN}
    # Each block's `location` is where that leg *ends*, so tapping it navigates
    # somewhere useful: the appointment on the way out, home on the way back.
    by_leg = {
        event["extendedProperties"]["private"][travel_blocks.LEG_KEY]: event
        for _, event in write.inserted
    }
    assert by_leg[LEG_OUTBOUND]["location"] == OFFICE
    assert by_leg[LEG_RETURN]["location"] == HOME
    assert all(event["summary"] == TITLE for _, event in write.inserted)
    assert payload["apply"]["counts"]["inserted"] == 2
    assert payload["apply"]["status"] == travel_blocks_write.APPLY_APPLIED
    assert write.closed is True


def test_the_existing_block_listing_is_marker_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A human event is never even fetched by the reconcile — Google filters it."""
    _, read, _ = _run(monkeypatch, tmp_path)
    assert [call["private_extended_property"] for call in read.list_kwargs] == [MARKER_FILTER]
    assert MARKER_FILTER == "wr_travel_block=1"
    assert read.closed is True


def test_rerunning_an_unchanged_sweep_performs_zero_calendar_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The acceptance criterion this whole design exists for."""
    first_payload, _, first_write = _run(monkeypatch, tmp_path)
    assert len(first_write.inserted) == 2

    # Feed the blocks it just wrote back in as the calendar's current contents.
    written = [
        {"id": f"blk-{index}", **event}
        for index, (_calendar_id, event) in enumerate(first_write.inserted)
    ]
    payload, _, write = _run(
        monkeypatch, tmp_path, read=_FakeReadClient({CALENDAR_ID: written})
    )

    assert write.inserted == [] and write.deleted == []
    assert payload["counts"] == {
        "desired": 2, "adds": 0, "deletes": 0, "keeps": 2, "protected": 0, "failures": 0
    }
    assert payload["apply"]["counts"]["inserted"] == 0
    assert payload["apply"]["counts"]["deleted"] == 0
    assert payload["apply"]["counts"]["kept"] == 2
    assert list(tmp_path.rglob("*.json")) == []
    assert first_payload["counts"]["adds"] == 2  # ...only the first run wrote anything


def test_a_moved_source_event_replaces_its_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Delete then re-insert, and the backup lands before the delete call."""
    _, _, first = _run(monkeypatch, tmp_path)
    written = [
        {"id": f"blk-{index}", **event} for index, (_c, event) in enumerate(first.inserted)
    ]

    payload, _, write = _run(
        monkeypatch,
        tmp_path,
        events={PERSON: [_raw_event("e1", start=_at(15))]},  # the meeting moved
        read=_FakeReadClient({CALENDAR_ID: written}),
    )

    assert [reason for reason in (d["reason"] for d in payload["deletes"])] == [
        travel_blocks.DELETE_REASON_REPLACED
    ] * 2
    assert len(write.deleted) == 2
    assert len(write.inserted) == 2
    assert write.backup_present_at_delete == [True, True]
    assert len(list(tmp_path.rglob("*.json"))) == 2
    assert payload["apply"]["counts"] == {
        "inserted": 2, "deleted": 2, "kept": 0, "skipped": 0, "backups": 2
    }


def test_a_cancelled_source_event_removes_its_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, _, first = _run(monkeypatch, tmp_path)
    written = [
        {"id": f"blk-{index}", **event} for index, (_c, event) in enumerate(first.inserted)
    ]

    payload, _, write = _run(
        monkeypatch,
        tmp_path,
        events={PERSON: []},  # the event is gone from the calendar
        read=_FakeReadClient({CALENDAR_ID: written}),
    )

    assert write.inserted == []
    assert len(write.deleted) == 2
    assert {d["reason"] for d in payload["deletes"]} == {travel_blocks.DELETE_REASON_ORPHANED}
    assert payload["apply"]["counts"]["backups"] == 2


def test_a_routes_outage_leaves_the_horizons_blocks_exactly_where_they_are(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transient `HTTP 429` must not delete a single block. The #267 regression.

    Everything else is unchanged and healthy: the source event still exists, is
    still in the horizon, the calendar reads fine and is writable. Only the
    pricing failed — so nothing is known about what these blocks *should* look
    like, and "unknown" is never applied as a delete. Before the fix this
    deleted both blocks as orphans and re-inserted nothing.
    """
    _, _, first = _run(monkeypatch, tmp_path)
    written = [
        {"id": f"blk-{index}", **event} for index, (_c, event) in enumerate(first.inserted)
    ]

    payload, _, write = _run(
        monkeypatch,
        tmp_path,
        read=_FakeReadClient({CALENDAR_ID: written}),
        route_fn=_StubRoutes(fails=True),
    )

    assert write.deleted == [] and write.inserted == []
    assert list(tmp_path.rglob("*.json")) == []  # nothing was even backed up
    assert payload["deletes"] == [] and payload["adds"] == []
    assert payload["apply"]["counts"] == {
        "inserted": 0, "deleted": 0, "kept": 0, "skipped": 0, "backups": 0
    }
    # ...and the outage is *reported*, never folded into a silent all-clear.
    assert payload["counts"]["protected"] == 2
    assert {f["reason"] for f in payload["failures"]} == {travel_blocks.FAILURE_ROUTES_ERROR}
    assert {block["event_id"] for block in payload["protected"]} == {"blk-0", "blk-1"}


def test_a_second_sweep_after_the_drive_departed_keeps_that_mornings_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Re-running the scan is safe at any hour — an elapsed block is left alone.

    Routes prices only future departures, so an afternoon sweep can price
    neither leg of a morning appointment. That is a failure to establish the
    drive, not a decision that the blocks are stale: deleting them would make
    the second sweep of the day destroy the first's correct work.
    """
    _, _, first = _run(monkeypatch, tmp_path)
    written = [
        {"id": f"blk-{index}", **event} for index, (_c, event) in enumerate(first.inserted)
    ]

    payload, _, write = _run(
        monkeypatch,
        tmp_path,
        read=_FakeReadClient({CALENDAR_ID: written}),
        now=DAY.replace(hour=20),  # the 09:00 appointment is long over
    )

    assert write.deleted == [] and write.inserted == []
    assert payload["counts"]["protected"] == 2
    assert {f["reason"] for f in payload["failures"]} == {
        travel_blocks.FAILURE_ANCHOR_IN_THE_PAST
    }


def test_an_event_ending_past_the_horizon_keeps_exactly_one_return_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The #272 regression: three sweeps, unchanged inputs, one block each way.

    An evening event on the last horizon day that runs past local midnight has
    its return block start at or after the horizon's end. Calendar's ``timeMax``
    is exclusive on an event's *start*, so the old listing — which stopped
    exactly at the horizon — never returned that block. The reconcile could not
    see what it had written, found no counterpart, and inserted a fresh one on
    every single sweep: one duplicate per run, stacked on the same evening slot.
    """
    _, horizon_end = _horizon()
    # Starts two hours inside the last horizon day, runs three: its return block
    # therefore starts an hour *past* the horizon's end.
    late = _raw_event("e1", start=horizon_end - timedelta(hours=2), hours=3)
    calendar_state: list[dict[str, Any]] = []
    written = 0
    inserts: list[int] = []
    deletes: list[int] = []

    for _ in range(3):
        _, _, write = _run(
            monkeypatch,
            tmp_path,
            events={PERSON: [late]},
            read=_FakeReadClient({CALENDAR_ID: list(calendar_state)}),
        )
        removed = {event_id for _calendar_id, event_id in write.deleted}
        calendar_state = [item for item in calendar_state if item["id"] not in removed]
        for _calendar_id, event in write.inserted:
            calendar_state.append({"id": f"blk-{written}", **event})
            written += 1
        inserts.append(len(write.inserted))
        deletes.append(len(write.deleted))

    assert inserts == [2, 0, 0], (
        f"expected one outbound + one return on the first sweep and nothing after it, "
        f"got {inserts} insert(s) per sweep: the return block of an event ending past the "
        f"horizon is being re-inserted on every run"
    )
    assert deletes == [0, 0, 0]
    assert len(calendar_state) == 2, (
        f"three sweeps with byte-identical inputs left {len(calendar_state)} blocks on the "
        f"calendar instead of 2 — one duplicate per run"
    )


def test_the_block_listing_reads_past_the_horizon_but_the_plan_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The read widens to the last block we could have written; the plan does not (#272)."""
    horizon_start, horizon_end = _horizon()
    late = _raw_event("e1", start=horizon_end - timedelta(hours=2), hours=3)
    payload, read, _ = _run(monkeypatch, tmp_path, events={PERSON: [late]})

    call = read.list_kwargs[0]
    assert call["time_min"] == horizon_start
    # The latest in-horizon source event ends an hour past the horizon, which is
    # exactly where its return block starts — plus the slack pad.
    assert call["time_max"] == horizon_end + timedelta(hours=1) + travel_blocks.LISTING_PAD
    # ...and the *planning* window is untouched: padding the read must never
    # make a block out there look desired, nor undesired.
    assert payload["horizon_end"] == horizon_end.isoformat()


def test_a_block_past_the_horizon_with_no_source_event_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The far worse failure the padding could have caused, asserted end to end.

    The padded read now surfaces blocks the sweep computes no desired state for.
    Every one of them must be reported and left exactly where it is — deleting
    them would be #272's fix causing the very data loss #267 was hardened against.
    """
    _, horizon_end = _horizon()
    beyond = horizon_end + timedelta(hours=6)
    stray = _raw_block(
        _leg(source_event_id="e-late", start=beyond, end=beyond + timedelta(minutes=20)),
        event_id="blk-late",
    )

    payload, _, write = _run(
        monkeypatch, tmp_path, read=_FakeReadClient({CALENDAR_ID: [stray]})
    )

    assert write.deleted == []
    assert payload["deletes"] == []
    assert list(tmp_path.rglob("*.json")) == []  # nothing was even backed up
    assert payload["counts"]["protected"] == 1
    assert payload["protected"] == [{
        "reason": travel_blocks.PROTECT_REASON_BEYOND_HORIZON,
        "calendar_id": CALENDAR_ID,
        "event_id": "blk-late",
        "source_event_id": "e-late",
        "leg": LEG_OUTBOUND,
        "start": beyond.isoformat(),
        "hash": stray["extendedProperties"]["private"][travel_blocks.HASH_KEY],
        "schema_version": travel_blocks.SCHEMA_VERSION,
    }]


def test_the_duplicates_this_bug_already_left_are_cleaned_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The migration path: a calendar that already accumulated copies converges.

    The padded read makes those stacked blocks visible again, and they resolve as
    what they are — duplicates of a leg that *is* desired, one of which matches
    the hash. That is a different judgement from the orphan sweep's, and the
    reason the guard spares only blocks with no desired counterpart at all.
    """
    _, horizon_end = _horizon()
    late = _raw_event("e1", start=horizon_end - timedelta(hours=2), hours=3)
    _, _, first = _run(monkeypatch, tmp_path, events={PERSON: [late]})
    written = [event for _calendar_id, event in first.inserted]
    returns = [
        event
        for event in written
        if event["extendedProperties"]["private"][travel_blocks.LEG_KEY] == LEG_RETURN
    ]
    assert len(returns) == 1
    # ...as the calendar looked after three pre-fix sweeps: two extra copies.
    stacked = [
        {"id": f"blk-{index}", **event}
        for index, event in enumerate([*written, *returns, *returns])
    ]

    payload, _, write = _run(
        monkeypatch,
        tmp_path,
        events={PERSON: [late]},
        read=_FakeReadClient({CALENDAR_ID: stacked}),
    )

    assert write.inserted == []
    assert len(write.deleted) == 2
    assert {d["reason"] for d in payload["deletes"]} == {travel_blocks.DELETE_REASON_DUPLICATE}
    assert payload["counts"] == {
        "desired": 2, "adds": 0, "deletes": 2, "keeps": 2, "protected": 0, "failures": 0
    }
    assert payload["apply"]["counts"]["backups"] == 2  # ...and each one was backed up first


def test_a_genuine_orphan_inside_the_horizon_is_still_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sparing the padded region must not switch orphan cleanup off inside it."""
    stale = _raw_block(_leg(source_event_id="e-gone"), event_id="blk-gone")

    payload, _, write = _run(
        monkeypatch,
        tmp_path,
        events={PERSON: []},
        read=_FakeReadClient({CALENDAR_ID: [stale]}),
    )

    assert [event_id for _calendar_id, event_id in write.deleted] == ["blk-gone"]
    assert [d["reason"] for d in payload["deletes"]] == [travel_blocks.DELETE_REASON_ORPHANED]
    assert payload["counts"]["protected"] == 0


def test_the_horizon_never_outruns_the_events_the_scan_fetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A wider horizon than the fetch would orphan-delete every block past it, every run."""
    import dataclasses
    import logging

    config = _config()
    family = dataclasses.replace(
        config.family,
        unknown_scan_days=3,
        assessment_days=2,
        travel_blocks=dataclasses.replace(config.family.travel_blocks, horizon_days=30),
    )
    wide = dataclasses.replace(config, family=family)

    start, end = travel_blocks.travel_block_horizon(wide, NOW)
    assert (end - start).days == 3 == travel_blocks.scan_window_days(wide)

    monkeypatch.setattr(
        calendar_source, "build_google_calendar_client", lambda _p: _FakeReadClient()
    )
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", lambda _p: _FakeWriteClient()
    )
    with caplog.at_level(logging.WARNING, logger="src.family.travel_blocks_write"):
        travel_blocks_write.run_travel_blocks(
            wide, {}, now=NOW, route_fn=_StubRoutes(), backup_root=tmp_path
        )
    # Clamped, and said so: a knob silently meaning something else is worse.
    assert any("horizon_days=30" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------- dry run


def test_a_dry_run_performs_no_insert_no_delete_and_no_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shipped default. Not "build a client and don't call it" — no client."""
    built = 0

    def refuse(_path: Path) -> Any:
        nonlocal built
        built += 1
        raise AssertionError("a dry run must never build a calendar write client")

    monkeypatch.setattr(travel_blocks_write, "build_google_calendar_write_client", refuse)
    monkeypatch.setattr(
        calendar_source, "build_google_calendar_client", lambda _p: _FakeReadClient()
    )
    from calendar_readonly.core import normalize_event

    payload = travel_blocks_write.run_travel_blocks(
        _config(dry_run=True),
        {PERSON: [normalize_event(_raw_event("e1"), calendar_id=CALENDAR_ID)]},
        now=NOW,
        route_fn=_StubRoutes(),
        backup_root=tmp_path,
    )

    assert built == 0
    assert payload["dry_run"] is True
    assert payload["counts"]["adds"] == 2  # the complete plan is still computed and logged
    assert payload["apply"]["status"] == travel_blocks_write.APPLY_DRY_RUN
    assert payload["apply"]["counts"] == {
        "inserted": 0, "deleted": 0, "kept": 0, "skipped": 2, "backups": 0
    }
    assert not tmp_path.exists() or list(tmp_path.rglob("*.json")) == []


def test_a_disabled_feature_reads_no_calendar_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The state of every default install must cost nothing, not even a listing."""

    def refuse(_path: Path) -> Any:
        raise AssertionError("a disabled feature must not read the calendar")

    monkeypatch.setattr(calendar_source, "build_google_calendar_client", refuse)
    payload = travel_blocks_write.run_travel_blocks(
        _config(enabled=False), {}, now=NOW, route_fn=_StubRoutes(), backup_root=tmp_path
    )
    assert payload == {"status": travel_blocks.STATUS_DISABLED}


# --------------------------------------------------------------- degradation, never a raise


def test_a_missing_write_token_degrades_to_a_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload, _, write = _run(
        monkeypatch, tmp_path, write_client_error=FileNotFoundError("write token missing")
    )
    assert write.inserted == []
    assert payload["apply"]["status"] == travel_blocks_write.APPLY_NO_WRITE_TOKEN
    assert "write token missing" in payload["apply"]["detail"]
    assert payload["apply"]["counts"]["skipped"] == 2


def test_a_failed_insert_degrades_that_block_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two calendars, one broken: the other person still gets their blocks."""
    accounts = (
        CalendarAccount(calendar_id=CALENDAR_ID, person=PERSON),
        CalendarAccount(calendar_id=CALENDAR_B, person=PERSON_B),
    )
    from calendar_readonly.core import normalize_event

    read_client = _FakeReadClient()
    write_client = _FakeWriteClient(insert_fails=[CALENDAR_B])
    write_client.backup_root = tmp_path
    monkeypatch.setattr(calendar_source, "build_google_calendar_client", lambda _p: read_client)
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", lambda _p: write_client
    )
    payload = travel_blocks_write.run_travel_blocks(
        _config(accounts=accounts),
        {
            PERSON: [normalize_event(_raw_event("e1"), calendar_id=CALENDAR_ID)],
            PERSON_B: [normalize_event(_raw_event("e2"), calendar_id=CALENDAR_B)],
        },
        now=NOW,
        route_fn=_StubRoutes(),
        backup_root=tmp_path,
    )

    assert [calendar_id for calendar_id, _ in write_client.inserted] == [CALENDAR_ID] * 2
    apply = payload["apply"]
    assert apply["counts"]["inserted"] == 2
    assert apply["counts"]["skipped"] == 2
    assert {f["reason"] for f in apply["failures"]} == {travel_blocks_write.FAILED_INSERT}
    assert {f["calendar_id"] for f in apply["failures"]} == {CALENDAR_B}


def test_a_failed_delete_holds_back_its_replacement_insert(tmp_path: Path) -> None:
    """Inserting over a block that is still there would duplicate it, not replace it."""
    stale = _block(_leg(minutes=20))
    desired = _leg(minutes=35)
    plan = travel_blocks.TravelBlockPlan(
        status=travel_blocks.STATUS_OK,
        dry_run=False,
        legs=[desired],
        adds=[desired],
        deletes=[travel_blocks.PlannedDelete(stale, travel_blocks.DELETE_REASON_REPLACED)],
        failures=[],
        routes_calls=0,
    )
    client = _FakeWriteClient(delete_fails=["blk-1"])
    client.backup_root = tmp_path
    writer = TravelBlockWriter(client, backup_root=tmp_path, now=NOW)

    result = apply_travel_blocks(
        plan,
        writer=writer,
        capability={CALENDAR_ID: travel_blocks_write.WRITABLE},
        title_template=TITLE,
    )

    assert client.inserted == [] and client.deleted == []
    assert result.inserted == 0 and result.deleted == 0 and result.skipped == 2
    assert [f["reason"] for f in result.failures] == [
        travel_blocks_write.FAILED_DELETE,
        travel_blocks_write.SKIP_STALE_BLOCK_REMAINS,
    ]


def test_the_marker_guard_degrades_the_run_instead_of_ending_it(tmp_path: Path) -> None:
    """It raises out of `delete_block` — and `apply` records it without crashing."""
    impostor = ExistingBlock(
        calendar_id=CALENDAR_ID,
        event_id="human-event",
        source_event_id="e1",
        leg=LEG_OUTBOUND,
        schema_version="1",
        stored_hash="h",
        resource=_raw_event("human-event"),
    )
    plan = travel_blocks.TravelBlockPlan(
        status=travel_blocks.STATUS_OK,
        dry_run=False,
        legs=[],
        adds=[],
        deletes=[travel_blocks.PlannedDelete(impostor, travel_blocks.DELETE_REASON_ORPHANED)],
        failures=[],
        routes_calls=0,
    )
    client = _FakeWriteClient()
    result = apply_travel_blocks(
        plan,
        writer=TravelBlockWriter(client, backup_root=tmp_path, now=NOW),
        capability={CALENDAR_ID: travel_blocks_write.WRITABLE},
        title_template=TITLE,
    )
    assert client.deleted == []
    assert result.deleted == 0 and result.skipped == 1
    assert [f["reason"] for f in result.failures] == [travel_blocks_write.FAILED_MARKER_GUARD]


def test_an_unreadable_calendar_is_left_alone_rather_than_re_added(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unknown current state must never be treated as an empty one."""
    payload, _, write = _run(
        monkeypatch, tmp_path, read=_FakeReadClient(list_fails=[CALENDAR_ID])
    )
    assert write.inserted == [] and write.deleted == []
    assert payload["adds"] == []
    assert payload["counts"]["desired"] == 2
    assert {f["reason"] for f in payload["failures"]} == {
        travel_blocks.FAILURE_BLOCKS_UNREADABLE
    }


def test_a_calendar_read_that_fails_entirely_reports_unknown_not_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even the read client failing to build must not read as "there are no blocks"."""

    def explode(_path: Path) -> Any:
        raise RuntimeError("read token revoked")

    write_client = _FakeWriteClient()
    monkeypatch.setattr(calendar_source, "build_google_calendar_client", explode)
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", lambda _p: write_client
    )
    from calendar_readonly.core import normalize_event

    payload = travel_blocks_write.run_travel_blocks(
        _config(),
        {PERSON: [normalize_event(_raw_event("e1"), calendar_id=CALENDAR_ID)]},
        now=NOW,
        route_fn=_StubRoutes(),
        backup_root=tmp_path,
    )

    assert write_client.inserted == [] and write_client.deleted == []
    assert payload["adds"] == [] and payload["deletes"] == []
    assert {f["reason"] for f in payload["failures"]} == {
        travel_blocks.FAILURE_BLOCKS_UNREADABLE
    }
    assert payload["apply"]["write_capability"] == {
        CALENDAR_ID: travel_blocks_write.WRITE_CAPABILITY_UNKNOWN
    }


def _stub_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire `run_calendar_scan` for offline use: fake read client, no events, no alert."""
    from src.family import calendar_scan

    monkeypatch.setattr(
        calendar_source, "build_google_calendar_client", lambda _p: _FakeReadClient()
    )
    monkeypatch.setattr(calendar_scan, "fetch_events_by_person", lambda *_a, **_k: {PERSON: []})
    monkeypatch.setattr(calendar_scan, "send_alert", lambda *_a, **_k: ("sent", None))


def test_the_scan_never_raises_when_the_write_side_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`run_calendar_scan`'s contract: a calendar-write problem is a payload field."""
    from src.family import calendar_scan

    def explode_write(_path: Path) -> Any:
        raise RuntimeError("write token revoked")

    _stub_scan(monkeypatch)
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", explode_write
    )

    # A *live* scan: only that reaches the write side at all now that
    # `--dry-run` forces the sweep dry (#276), and reaching the write side is
    # this test's whole point.
    payload = calendar_scan.run_calendar_scan(_config(), now=NOW, dry_run=False)
    assert payload["status"] == "ok"
    assert payload["travel_blocks"]["status"] == travel_blocks.STATUS_OK
    assert payload["travel_blocks"]["apply"]["status"] == travel_blocks_write.APPLY_NO_WRITE_TOKEN


# --------------------------------------------------------------- issue #276


def test_scan_dry_run_forces_the_sweep_dry_even_when_configured_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`calendar-scan --dry-run` must not write to a calendar. Ever.

    Before #276 this verb's ``--dry-run`` suppressed only the summary alert:
    the sweep riding along inside it still inserted and deleted real events
    whenever ``travel_blocks.dry_run`` was off. The Family tab's rehearse
    control fires exactly this, so the refusal has to live here — in the server
    — not in whether a button was disabled.
    """
    from src.family import calendar_scan

    _stub_scan(monkeypatch)
    write_client = _FakeWriteClient()
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", lambda _p: write_client
    )

    live_config = _config(dry_run=False)
    payload = calendar_scan.run_calendar_scan(live_config, now=NOW, dry_run=True)

    section = payload["travel_blocks"]
    assert section["status"] == travel_blocks.STATUS_OK
    # The plan says it was a rehearsal, and the apply never left the short-circuit.
    assert section["dry_run"] is True
    assert section["apply"]["status"] == travel_blocks_write.APPLY_DRY_RUN
    assert write_client.inserted == [] and write_client.deleted == []
    # The override only ever tightens: a live scan on the same config still writes.
    assert live_config.family.travel_blocks.dry_run is False


def test_force_dry_run_cannot_loosen_a_dry_run_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``force_dry_run=False`` is "no opinion", never "go live"."""
    write_client = _FakeWriteClient()
    monkeypatch.setattr(
        calendar_source, "build_google_calendar_client", lambda _p: _FakeReadClient()
    )
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", lambda _p: write_client
    )
    payload = travel_blocks_write.run_travel_blocks(
        _config(dry_run=True),
        {PERSON: []},
        now=NOW,
        route_fn=_StubRoutes(),
        backup_root=tmp_path,
        force_dry_run=False,
    )
    assert payload["dry_run"] is True
    assert payload["apply"]["status"] == travel_blocks_write.APPLY_DRY_RUN
    assert write_client.inserted == [] and write_client.deleted == []


def test_a_gated_sweep_forced_dry_still_reports_its_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rehearsal of a disabled feature reports `disabled`, not an empty plan."""
    payload = travel_blocks_write.run_travel_blocks(
        _config(enabled=False, dry_run=False),
        {PERSON: []},
        now=NOW,
        route_fn=_StubRoutes(),
        backup_root=tmp_path,
        force_dry_run=True,
    )
    assert payload == {"status": travel_blocks.STATUS_DISABLED}


# --------------------------------------------------------------- issue #273


def test_duplicate_calendar_id_across_accounts_stops_churning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A calendar_id repeated across two `calendar.accounts` entries must not churn.

    Reproduces the reported mechanism end to end, through the real read seam:
    `fetch_events_by_person` iterates `calendar.accounts` once per entry, so
    two entries pointing at the same physical calendar used to pull the same
    event twice, tagged with an identical `calendar_id`. Since `leg_key` is
    `(calendar_id, source_event_id, leg)`, the two duplicate desired legs
    collided — `reconcile` never dedupes the desired side (a duplicate there is
    a bug upstream, not something to paper over, per its own docstring), so
    every sweep after the first deleted one of the pair and re-inserted it,
    forever: exactly the "2 deletes + 2 inserts on every run" this issue
    reports.

    The fix collapses the duplicate account at config-parse time
    (`src.config.calendar.parse`), so the fetch only ever sees the calendar
    once and the collision never reaches `reconcile` at all.
    """
    calendar_cfg = parse_calendar_accounts(
        {
            "accounts": [
                {"calendar_id": CALENDAR_ID, "person": PERSON, "label": "Parent A"},
                {
                    "calendar_id": CALENDAR_ID,
                    "person": PERSON_B,
                    "label": "Parent A's calendar, mistakenly also under Parent B",
                },
            ]
        },
        tmp_path,
    )

    def _fetch() -> dict[str, list[Any]]:
        client = _FakeReadClient({CALENDAR_ID: [_raw_event("e1")]})
        monkeypatch.setattr(calendar_source, "build_google_calendar_client", lambda _p: client)
        return calendar_source.fetch_events_by_person(
            calendar_cfg, time_min=DAY, time_max=DAY + timedelta(days=2)
        )

    def _sweep(
        events: dict[str, list[Any]], marked: dict[str, list[dict[str, Any]]]
    ) -> tuple[dict[str, Any], _FakeWriteClient]:
        read_client = _FakeReadClient(marked)
        write_client = _FakeWriteClient()
        write_client.backup_root = tmp_path
        monkeypatch.setattr(calendar_source, "build_google_calendar_client", lambda _p: read_client)
        monkeypatch.setattr(
            travel_blocks_write, "build_google_calendar_write_client", lambda _p: write_client
        )
        payload = travel_blocks_write.run_travel_blocks(
            _config(accounts=calendar_cfg.accounts),
            events,
            now=NOW,
            route_fn=_StubRoutes(),
            backup_root=tmp_path,
        )
        return payload, write_client

    events = _fetch()
    first_payload, first_write = _sweep(events, marked={})
    # Self-guard: this test means nothing if the fixture wrote no blocks at all.
    assert first_write.inserted, "the fixture must produce at least one travel block"
    assert first_payload["counts"]["adds"] == len(first_write.inserted)

    written = [
        {"id": f"blk-{index}", **event}
        for index, (_calendar_id, event) in enumerate(first_write.inserted)
    ]
    second_payload, second_write = _sweep(events, marked={CALENDAR_ID: written})

    # The acceptance criterion #273 exists for: unchanged inputs, the second
    # sweep must write nothing at all.
    assert second_write.inserted == [] and second_write.deleted == [], (
        "expected zero churn on an unchanged second sweep, got "
        f"{len(second_write.inserted)} insert(s) and {len(second_write.deleted)} "
        "delete(s) — a calendar_id duplicated across `calendar.accounts` entries "
        "must be collapsed before it ever reaches the travel-blocks reconcile"
    )
    assert second_payload["counts"]["adds"] == 0 and second_payload["counts"]["deletes"] == 0


# --------------------------------------------------------------- the start edge (#281)


def test_a_block_that_already_ended_is_never_listed_and_so_never_retro_cleaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sweep does not look backwards — decided and documented, not accidental (#281).

    The marker-scoped listing starts at ``horizon_start`` (today's local
    midnight) and Google's ``timeMin`` is an exclusive lower bound on an event's
    **end**, so a block that ended before that instant is not returned at all.
    Its source event can be long gone and the orphan sweep still never sees it:
    past blocks are not retro-cleaned. The cut is *midnight*, not *now* — a
    block that ended earlier today is still listed and still judged, which is
    what the sibling test below asserts.

    That is the shipped behaviour and #281 chose to keep it. Widening ``timeMin``
    backwards would make every historical block this feature ever wrote visible
    to the orphan sweep on the first run after the change — a mass delete
    dressed as a cleanup. This test pins the boundary so that widening cannot
    happen by accident: if someone moves ``time_min`` earlier without reading
    #281, this fails and says why. README documents the by-hand removal path
    (the ``wr_travel_block=1`` marker filter, runbook step 7).
    """
    horizon_start, _ = _horizon()
    # A block whose source event is gone, finishing before the window opens.
    departed = _leg(
        source_event_id="e-yesterday",
        start=horizon_start - timedelta(hours=3),
        end=horizon_start - timedelta(hours=2, minutes=40),
    )
    read = _FakeReadClient({CALENDAR_ID: [_raw_block(departed, event_id="blk-past")]})

    payload, read_client, write_client = _run(
        monkeypatch, tmp_path, events={PERSON: []}, read=read
    )

    # The harm first, so a future failure names it rather than the mechanism:
    # nothing historical may be removed, and not `protected` either — that would
    # wrongly imply the sweep considered this block and spared it.
    assert payload["deletes"] == [], "a past block was judged; see #281 before widening"
    assert payload["protected"] == []
    assert write_client.deleted == []
    # And the mechanism that guarantees it: the listing opens exactly at the
    # horizon and never earlier, so the block was not even fetched.
    assert read_client.list_kwargs, "the sweep must actually have listed"
    assert all(kw["time_min"] == horizon_start for kw in read_client.list_kwargs)


def test_the_same_block_still_inside_the_window_is_deleted_as_an_orphan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half of #281's split, asserted so the asymmetry is on the record.

    Identical block, identical departed source event — only the time of day
    differs. This one still ends after ``horizon_start``, so it *is* listed and
    *is* orphan-deleted. Two blocks of one departed event getting opposite fates
    is the behaviour README now states plainly rather than leaving to be
    discovered.
    """
    horizon_start, _ = _horizon()
    still_visible = _leg(
        source_event_id="e-yesterday",
        start=horizon_start + timedelta(hours=1),
        end=horizon_start + timedelta(hours=1, minutes=20),
    )
    read = _FakeReadClient({CALENDAR_ID: [_raw_block(still_visible, event_id="blk-today")]})

    payload, _read_client, write_client = _run(
        monkeypatch, tmp_path, events={PERSON: []}, read=read
    )

    assert [d["reason"] for d in payload["deletes"]] == [travel_blocks.DELETE_REASON_ORPHANED]
    assert [event_id for _cal, event_id in write_client.deleted] == ["blk-today"]


# --------------------------------------------------------------- error detail privacy (#285)

#: A calendar id planted where a raw exception string would carry one. Asserted
#: on as a sentinel, never as an email shape: `@` only rules out email-shaped
#: values, and the write path's exceptions also carry request URIs and backup
#: file paths built from the same id.
LEAKY_ID = "SENTINELLEAKYCALENDAR@leak.invalid"


class _LeakyWriteClient(_FakeWriteClient):
    """A write client whose failures echo the calendar id, as the real ones do.

    Not invented for the test: `MarkerGuardError` formats two calendar ids into
    its message verbatim, and a propagated ``googleapiclient`` ``HttpError``
    stringifies to the request URI, which contains the URL-encoded id.
    """

    def insert_event(self, *, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(
            f"<HttpError 403 when requesting https://www.googleapis.com/calendar/v3/"
            f"calendars/{LEAKY_ID}/events?alt=json returned \"Forbidden\">"
        )

    def delete_event(self, *, calendar_id: str, event_id: str) -> None:
        raise RuntimeError(
            f"refusing to delete calendar event {event_id!r} on {LEAKY_ID!r}: "
            f"the marked resource belongs to calendar {LEAKY_ID!r}"
        )


def test_a_write_failure_never_persists_the_calendar_id_in_its_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The persisted `detail` is sanitized at source, not at the renderer (#285).

    The Audit tab's payload dump renders `detail` verbatim — it is genuine
    diagnostic content and withholding it would gut the dump. That is only safe
    because the raw exception never reaches the payload in the first place, the
    same contract the *read* path has had since `safe_error_detail` was written
    (`src/family/calendar_source.py`). This is where that contract is enforced.

    The full exception text is still logged at every one of these sites, so
    nothing is lost — it simply stays in a local log instead of being painted
    into a DOM that is reachable over Tailscale.
    """
    payload, _read, _write = _run(
        monkeypatch, tmp_path, write=_LeakyWriteClient(), dry_run=False
    )

    serialized = json.dumps(payload)
    assert LEAKY_ID not in serialized, "a raw exception string carried the calendar id"
    # The failure is still *reported* — sanitizing must not silence it.
    failures = payload["apply"]["failures"]
    assert failures, "the write failure must still be recorded"
    assert all(f["detail"] for f in failures), "a failure with no detail is unactionable"
    assert all(LEAKY_ID not in (f["detail"] or "") for f in failures)


def test_no_write_failure_detail_is_built_from_a_raw_exception(tmp_path: Path) -> None:
    """Structural, because the guarantee is "there is no such code path" (#285).

    One `detail=str(exc)` reintroduced anywhere on the write path puts calendar
    ids back into the Audit dump, and no amount of stubbing would notice a site
    that this suite happens not to drive.
    """
    root = Path(__file__).resolve().parents[1]
    source = (root / "src/family/travel_blocks_write.py").read_text(encoding="utf-8")
    assert "str(exc)" not in source, (
        "a persisted `detail` must go through calendar_readonly.safe_error_detail — "
        "see #285; the raw text still goes to the log"
    )
    assert "safe_error_detail" in source
