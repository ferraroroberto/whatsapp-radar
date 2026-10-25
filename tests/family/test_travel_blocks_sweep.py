"""The live and dry-run travel-block sweep, and how it degrades without raising.

Doubles live in :mod:`tests.fakes.travel_block_doubles`.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from src.config import (
    CalendarAccount,
)
from src.family import calendar_source, travel_blocks, travel_blocks_write
from src.family.travel_blocks import (
    LEG_OUTBOUND,
    LEG_RETURN,
    MARKER_FILTER,
    ExistingBlock,
)
from src.family.travel_blocks_write import (
    TravelBlockWriter,
    apply_travel_blocks,
)
from tests.fakes.travel_block_doubles import (
    CALENDAR_B,
    CALENDAR_ID,
    DAY,
    HOME,
    NOW,
    OFFICE,
    PERSON,
    PERSON_B,
    TITLE,
    FakeReadClient,
    FakeWriteClient,
    StubRoutes,
    at_hour,
    make_block,
    make_config,
    make_leg,
    make_raw_block,
    make_raw_event,
    run_sweep,
    stub_scan,
    sweep_horizon,
)

# --------------------------------------------------------------- the live sweep


def test_a_live_sweep_writes_an_outbound_and_a_return_on_the_persons_own_calendar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload, read, write = run_sweep(monkeypatch, tmp_path)

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
    _, read, _ = run_sweep(monkeypatch, tmp_path)
    assert [call["private_extended_property"] for call in read.list_kwargs] == [MARKER_FILTER]
    assert MARKER_FILTER == "wr_travel_block=1"
    assert read.closed is True


def test_rerunning_an_unchanged_sweep_performs_zero_calendar_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The acceptance criterion this whole design exists for."""
    first_payload, _, first_write = run_sweep(monkeypatch, tmp_path)
    assert len(first_write.inserted) == 2

    # Feed the blocks it just wrote back in as the calendar's current contents.
    written = [
        {"id": f"blk-{index}", **event}
        for index, (_calendar_id, event) in enumerate(first_write.inserted)
    ]
    payload, _, write = run_sweep(
        monkeypatch, tmp_path, read=FakeReadClient({CALENDAR_ID: written})
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
    _, _, first = run_sweep(monkeypatch, tmp_path)
    written = [
        {"id": f"blk-{index}", **event} for index, (_c, event) in enumerate(first.inserted)
    ]

    payload, _, write = run_sweep(
        monkeypatch,
        tmp_path,
        events={PERSON: [make_raw_event("e1", start=at_hour(15))]},  # the meeting moved
        read=FakeReadClient({CALENDAR_ID: written}),
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
    _, _, first = run_sweep(monkeypatch, tmp_path)
    written = [
        {"id": f"blk-{index}", **event} for index, (_c, event) in enumerate(first.inserted)
    ]

    payload, _, write = run_sweep(
        monkeypatch,
        tmp_path,
        events={PERSON: []},  # the event is gone from the calendar
        read=FakeReadClient({CALENDAR_ID: written}),
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
    _, _, first = run_sweep(monkeypatch, tmp_path)
    written = [
        {"id": f"blk-{index}", **event} for index, (_c, event) in enumerate(first.inserted)
    ]

    payload, _, write = run_sweep(
        monkeypatch,
        tmp_path,
        read=FakeReadClient({CALENDAR_ID: written}),
        route_fn=StubRoutes(fails=True),
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
    _, _, first = run_sweep(monkeypatch, tmp_path)
    written = [
        {"id": f"blk-{index}", **event} for index, (_c, event) in enumerate(first.inserted)
    ]

    payload, _, write = run_sweep(
        monkeypatch,
        tmp_path,
        read=FakeReadClient({CALENDAR_ID: written}),
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
    _, horizon_end = sweep_horizon()
    # Starts two hours inside the last horizon day, runs three: its return block
    # therefore starts an hour *past* the horizon's end.
    late = make_raw_event("e1", start=horizon_end - timedelta(hours=2), hours=3)
    calendar_state: list[dict[str, Any]] = []
    written = 0
    inserts: list[int] = []
    deletes: list[int] = []

    for _ in range(3):
        _, _, write = run_sweep(
            monkeypatch,
            tmp_path,
            events={PERSON: [late]},
            read=FakeReadClient({CALENDAR_ID: list(calendar_state)}),
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
    horizon_start, horizon_end = sweep_horizon()
    late = make_raw_event("e1", start=horizon_end - timedelta(hours=2), hours=3)
    payload, read, _ = run_sweep(monkeypatch, tmp_path, events={PERSON: [late]})

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
    _, horizon_end = sweep_horizon()
    beyond = horizon_end + timedelta(hours=6)
    stray = make_raw_block(
        make_leg(source_event_id="e-late", start=beyond, end=beyond + timedelta(minutes=20)),
        event_id="blk-late",
    )

    payload, _, write = run_sweep(
        monkeypatch, tmp_path, read=FakeReadClient({CALENDAR_ID: [stray]})
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
    _, horizon_end = sweep_horizon()
    late = make_raw_event("e1", start=horizon_end - timedelta(hours=2), hours=3)
    _, _, first = run_sweep(monkeypatch, tmp_path, events={PERSON: [late]})
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

    payload, _, write = run_sweep(
        monkeypatch,
        tmp_path,
        events={PERSON: [late]},
        read=FakeReadClient({CALENDAR_ID: stacked}),
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
    stale = make_raw_block(make_leg(source_event_id="e-gone"), event_id="blk-gone")

    payload, _, write = run_sweep(
        monkeypatch,
        tmp_path,
        events={PERSON: []},
        read=FakeReadClient({CALENDAR_ID: [stale]}),
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

    config = make_config()
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
        calendar_source, "build_google_calendar_client", lambda _p: FakeReadClient()
    )
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", lambda _p: FakeWriteClient()
    )
    with caplog.at_level(logging.WARNING, logger="src.family.travel_blocks_write"):
        travel_blocks_write.run_travel_blocks(
            wide, {}, now=NOW, route_fn=StubRoutes(), backup_root=tmp_path
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
        calendar_source, "build_google_calendar_client", lambda _p: FakeReadClient()
    )
    from calendar_readonly.core import normalize_event

    payload = travel_blocks_write.run_travel_blocks(
        make_config(dry_run=True),
        {PERSON: [normalize_event(make_raw_event("e1"), calendar_id=CALENDAR_ID)]},
        now=NOW,
        route_fn=StubRoutes(),
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
        make_config(enabled=False), {}, now=NOW, route_fn=StubRoutes(), backup_root=tmp_path
    )
    assert payload == {"status": travel_blocks.STATUS_DISABLED}


# --------------------------------------------------------------- degradation, never a raise


def test_a_missing_write_token_degrades_to_a_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload, _, write = run_sweep(
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

    read_client = FakeReadClient()
    write_client = FakeWriteClient(insert_fails=[CALENDAR_B])
    write_client.backup_root = tmp_path
    monkeypatch.setattr(calendar_source, "build_google_calendar_client", lambda _p: read_client)
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", lambda _p: write_client
    )
    payload = travel_blocks_write.run_travel_blocks(
        make_config(accounts=accounts),
        {
            PERSON: [normalize_event(make_raw_event("e1"), calendar_id=CALENDAR_ID)],
            PERSON_B: [normalize_event(make_raw_event("e2"), calendar_id=CALENDAR_B)],
        },
        now=NOW,
        route_fn=StubRoutes(),
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
    stale = make_block(make_leg(minutes=20))
    desired = make_leg(minutes=35)
    plan = travel_blocks.TravelBlockPlan(
        status=travel_blocks.STATUS_OK,
        dry_run=False,
        legs=[desired],
        adds=[desired],
        deletes=[travel_blocks.PlannedDelete(stale, travel_blocks.DELETE_REASON_REPLACED)],
        failures=[],
        routes_calls=0,
    )
    client = FakeWriteClient(delete_fails=["blk-1"])
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
        resource=make_raw_event("human-event"),
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
    client = FakeWriteClient()
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
    payload, _, write = run_sweep(
        monkeypatch, tmp_path, read=FakeReadClient(list_fails=[CALENDAR_ID])
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

    write_client = FakeWriteClient()
    monkeypatch.setattr(calendar_source, "build_google_calendar_client", explode)
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", lambda _p: write_client
    )
    from calendar_readonly.core import normalize_event

    payload = travel_blocks_write.run_travel_blocks(
        make_config(),
        {PERSON: [normalize_event(make_raw_event("e1"), calendar_id=CALENDAR_ID)]},
        now=NOW,
        route_fn=StubRoutes(),
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


def test_the_scan_never_raises_when_the_write_side_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`run_calendar_scan`'s contract: a calendar-write problem is a payload field."""
    from src.family import calendar_scan

    def explode_write(_path: Path) -> Any:
        raise RuntimeError("write token revoked")

    stub_scan(monkeypatch)
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", explode_write
    )

    # A *live* scan: only that reaches the write side at all now that
    # `--dry-run` forces the sweep dry (#276), and reaching the write side is
    # this test's whole point.
    payload = calendar_scan.run_calendar_scan(make_config(), now=NOW, dry_run=False)
    assert payload["status"] == "ok"
    assert payload["travel_blocks"]["status"] == travel_blocks.STATUS_OK
    assert payload["travel_blocks"]["apply"]["status"] == travel_blocks_write.APPLY_NO_WRITE_TOKEN
