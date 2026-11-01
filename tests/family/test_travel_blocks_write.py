"""Reconcile, block resource, delete guard and write capability for travel blocks.

The delete path is what these tests exist for: behaviour (a marked block is
deleted, an unmarked resource is refused loudly), ordering (the backup is on disk
*before* the API call) and structure (there is no reachable spelling of
``delete_event`` that skips the guard). Doubles live in
:mod:`tests.fakes.travel_block_doubles`.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from src.family import travel_blocks, travel_blocks_write
from src.family.travel_blocks import (
    LEG_OUTBOUND,
    ExistingBlock,
    reconcile,
)
from src.family.travel_blocks_write import (
    MarkerGuardError,
    TravelBlockWriter,
    apply_travel_blocks,
    classify_access_role,
)
from tests.fakes.travel_block_doubles import (
    CALENDAR_B,
    CALENDAR_ID,
    CLINIC,
    NOW,
    PLAN_END,
    TITLE,
    FakeReadClient,
    FakeWriteClient,
    make_block,
    make_leg,
    make_raw_block,
    make_raw_event,
    run_sweep,
)

# --------------------------------------------------------------- the reconcile (pure)


def test_a_missing_block_is_an_add() -> None:
    leg = make_leg()
    diff = reconcile([leg], [], plan_end=PLAN_END)
    assert diff.adds == [leg]
    assert diff.deletes == [] and diff.keeps == []


def test_an_unchanged_block_is_kept_and_never_rewritten() -> None:
    """The zero-write contract, at its source: same hash, same schema, no ops."""
    leg = make_leg()
    diff = reconcile([leg], [make_block(leg)], plan_end=PLAN_END)
    assert diff.adds == [] and diff.deletes == []
    assert [block.event_id for block in diff.keeps] == ["blk-1"]


def test_a_changed_hash_is_a_delete_and_a_reinsert() -> None:
    """A moved source event, or a new location, invalidates the stored hash."""
    stale = make_block(make_leg(minutes=20))
    desired = make_leg(minutes=35)  # a different drive length => a different hash
    diff = reconcile([desired], [stale], plan_end=PLAN_END)
    assert diff.adds == [desired]
    assert [(d.block.event_id, d.reason) for d in diff.deletes] == [
        ("blk-1", travel_blocks.DELETE_REASON_REPLACED)
    ]
    assert diff.keeps == []


def test_a_vanished_source_event_leaves_an_orphan_to_delete() -> None:
    """Cancelled, or simply out of the horizon: nothing desires this block now."""
    diff = reconcile([], [make_block(make_leg())], plan_end=PLAN_END)
    assert diff.adds == [] and diff.keeps == []
    assert [d.reason for d in diff.deletes] == [travel_blocks.DELETE_REASON_ORPHANED]


def test_an_unrecognised_schema_version_is_never_a_keep() -> None:
    """A build that cannot vouch for a block's shape must not certify it."""
    leg = make_leg()
    raw = make_raw_block(leg)
    raw["extendedProperties"]["private"]["wr_schema_version"] = "99"
    stale = travel_blocks.parse_existing_block(raw, calendar_id=CALENDAR_ID)
    assert stale is not None
    diff = reconcile([leg], [stale], plan_end=PLAN_END)
    assert diff.adds == [leg]
    assert [d.reason for d in diff.deletes] == [travel_blocks.DELETE_REASON_REPLACED]


def test_a_duplicate_block_for_one_leg_is_removed_not_tolerated() -> None:
    leg = make_leg()
    diff = reconcile(
        [leg],
        [make_block(leg, event_id="blk-1"), make_block(leg, event_id="blk-2")],
        plan_end=PLAN_END,
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
    leg = make_leg()
    block = make_block(leg)

    diff = reconcile([], [block], plan_end=PLAN_END, protected={leg.key})

    assert diff.deletes == [] and diff.adds == [] and diff.keeps == []
    # Left alone, and said out loud — with *which* unestablished fact spared it.
    assert [(p.block, p.reason) for p in diff.protected] == [
        (block, travel_blocks.PROTECT_REASON_UNPLANNED_LEG)
    ]


def test_protection_covers_only_the_leg_that_failed() -> None:
    """One unplannable leg must not shield a genuinely orphaned block on the same calendar."""
    failed = make_leg(source_event_id="e1")
    orphan = make_block(make_leg(source_event_id="e2"), event_id="blk-2")

    diff = reconcile(
        [], [make_block(failed), orphan], plan_end=PLAN_END, protected={failed.key}
    )

    assert [pending.block for pending in diff.deletes] == [orphan]
    assert [pending.reason for pending in diff.deletes] == [
        travel_blocks.DELETE_REASON_ORPHANED
    ]
    assert [pending.block for pending in diff.protected] == [make_block(failed)]


def test_an_orphan_past_the_plan_window_is_left_alone_not_deleted() -> None:
    """#272's trap: the padded read must widen what we *see*, never what we judge.

    A block starting past ``plan_end`` was only fetched because the listing now
    reads further than the plan does. Nothing computed a desired counterpart out
    there, so "no counterpart" is not evidence of an orphan — and treating it as
    one would turn a fix for duplicate blocks into a delete of correct ones.
    """
    beyond = make_block(
        make_leg(source_event_id="e-late", start=PLAN_END + timedelta(hours=1),
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
    edge = make_block(
        make_leg(source_event_id="e-edge", start=PLAN_END, end=PLAN_END + timedelta(minutes=20)),
        event_id="blk-edge",
    )
    inside = make_block(
        make_leg(source_event_id="e-in", start=PLAN_END - timedelta(minutes=1),
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
    raw = make_raw_block(make_leg())
    raw["start"] = {"date": "2026-07-20"}  # all-day: no moment to compare at all
    block = travel_blocks.parse_existing_block(raw, calendar_id=CALENDAR_ID)
    assert block is not None

    diff = reconcile([], [block], plan_end=PLAN_END)

    assert diff.deletes == []
    assert [p.reason for p in diff.protected] == [travel_blocks.PROTECT_REASON_START_UNKNOWN]


def test_parse_refuses_a_resource_without_the_marker() -> None:
    """The only constructor the delete path accepts is itself marker-checking."""
    assert travel_blocks.parse_existing_block(make_raw_event("e1"), calendar_id=CALENDAR_ID) is None


# --------------------------------------------------------------- the block resource


def test_the_written_block_is_private_busy_and_silent() -> None:
    event = travel_blocks.build_block_event(make_leg(destination=CLINIC), title_template=TITLE)
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
    client = FakeWriteClient()
    writer = TravelBlockWriter(client, backup_root=tmp_path, now=NOW)
    impostor = ExistingBlock(
        calendar_id=CALENDAR_ID,
        event_id="human-event",
        source_event_id="e1",
        leg=LEG_OUTBOUND,
        schema_version="1",
        stored_hash="whatever",
        resource=make_raw_event("human-event"),  # a real human event: no marker
    )
    with pytest.raises(MarkerGuardError) as caught:
        writer.delete_block(impostor, reason="orphaned")
    assert travel_blocks.MARKER_KEY in str(caught.value)
    assert client.deleted == []
    assert list(tmp_path.rglob("*.json")) == []  # not even a backup was written


def test_a_wrong_marker_value_is_refused_too(tmp_path: Path) -> None:
    """`wr_travel_block: "0"` is not ours either — the check is on the value."""
    raw = make_raw_event("x")
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
    writer = TravelBlockWriter(FakeWriteClient(), backup_root=tmp_path, now=NOW)
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
    client = FakeWriteClient()
    writer = TravelBlockWriter(client, backup_root=tmp_path, now=NOW)
    marked = make_block(make_leg(), event_id="blk-1")
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
    client = FakeWriteClient()
    writer = TravelBlockWriter(client, backup_root=tmp_path, now=NOW)
    marked = make_block(make_leg())
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
    client = FakeWriteClient()
    client.backup_root = tmp_path
    writer = TravelBlockWriter(client, backup_root=tmp_path, now=NOW)
    block = make_block(make_leg())

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
    client = FakeWriteClient()
    writer = TravelBlockWriter(client, backup_root=tmp_path, now=NOW)

    def explode(*_args: object, **_kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(travel_blocks_write, "write_backup", explode)
    block = make_block(make_leg())
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
    root = Path(__file__).resolve().parents[2]
    product_specific = ("wr_travel_block", "wr_source_event_id", "Trayecto", "calendar_backups")
    for package in ("calendar_readonly", "calendar_write"):
        for path in (root / "src" / package).glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("from src", "import src", "from app", "import app", "from scripts"):
                assert forbidden not in source, f"{path.name} must not import {forbidden}"
            for line in source.splitlines():
                if line.lstrip().startswith("from .."):
                    assert line.lstrip().startswith("from ..google_oauth_common"), path.name
            for term in product_specific:
                assert term not in source, f"{path.name} must not name {term}"


def test_delete_event_is_reachable_only_through_the_guarded_writer() -> None:
    """Structural, not behavioural: no stub can prove the absence of a code path.

    Exactly one call site in the whole feature, inside `delete_block`, and the
    marker check and the backup both precede it there. Reordering them, or
    adding a second call site, fails this test rather than a code review.
    """
    root = Path(__file__).resolve().parents[2]
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
    payload, _, write = run_sweep(
        monkeypatch, tmp_path, read=FakeReadClient(roles={CALENDAR_ID: "reader"})
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
    payload, _, write = run_sweep(
        monkeypatch, tmp_path, read=FakeReadClient(role_fails=[CALENDAR_ID])
    )
    apply = payload["apply"]
    assert write.inserted == []
    assert apply["write_capability"] == {CALENDAR_ID: travel_blocks_write.WRITE_CAPABILITY_UNKNOWN}
    assert {f["reason"] for f in apply["failures"]} == {
        travel_blocks_write.SKIP_CAPABILITY_UNKNOWN
    }
