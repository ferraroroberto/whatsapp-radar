"""Scan dry-run, duplicate-calendar, window-edge and error-detail privacy regressions.

Doubles live in :mod:`tests.fakes.travel_block_doubles`.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from src.config.calendar import parse as parse_calendar_accounts
from src.family import calendar_source, travel_blocks, travel_blocks_write
from tests.fakes.travel_block_doubles import (
    CALENDAR_ID,
    DAY,
    NOW,
    PERSON,
    PERSON_B,
    FakeReadClient,
    FakeWriteClient,
    StubRoutes,
    make_config,
    make_leg,
    make_raw_block,
    make_raw_event,
    run_sweep,
    stub_scan,
    sweep_horizon,
)

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

    stub_scan(monkeypatch)
    write_client = FakeWriteClient()
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", lambda _p: write_client
    )

    live_config = make_config(dry_run=False)
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
    write_client = FakeWriteClient()
    monkeypatch.setattr(
        calendar_source, "build_google_calendar_client", lambda _p: FakeReadClient()
    )
    monkeypatch.setattr(
        travel_blocks_write, "build_google_calendar_write_client", lambda _p: write_client
    )
    payload = travel_blocks_write.run_travel_blocks(
        make_config(dry_run=True),
        {PERSON: []},
        now=NOW,
        route_fn=StubRoutes(),
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
        make_config(enabled=False, dry_run=False),
        {PERSON: []},
        now=NOW,
        route_fn=StubRoutes(),
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
        client = FakeReadClient({CALENDAR_ID: [make_raw_event("e1")]})
        monkeypatch.setattr(calendar_source, "build_google_calendar_client", lambda _p: client)
        return calendar_source.fetch_events_by_person(
            calendar_cfg, time_min=DAY, time_max=DAY + timedelta(days=2)
        )

    def _sweep(
        events: dict[str, list[Any]], marked: dict[str, list[dict[str, Any]]]
    ) -> tuple[dict[str, Any], FakeWriteClient]:
        read_client = FakeReadClient(marked)
        write_client = FakeWriteClient()
        write_client.backup_root = tmp_path
        monkeypatch.setattr(calendar_source, "build_google_calendar_client", lambda _p: read_client)
        monkeypatch.setattr(
            travel_blocks_write, "build_google_calendar_write_client", lambda _p: write_client
        )
        payload = travel_blocks_write.run_travel_blocks(
            make_config(accounts=calendar_cfg.accounts),
            events,
            now=NOW,
            route_fn=StubRoutes(),
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
    horizon_start, _ = sweep_horizon()
    # A block whose source event is gone, finishing before the window opens.
    departed = make_leg(
        source_event_id="e-yesterday",
        start=horizon_start - timedelta(hours=3),
        end=horizon_start - timedelta(hours=2, minutes=40),
    )
    read = FakeReadClient({CALENDAR_ID: [make_raw_block(departed, event_id="blk-past")]})

    payload, read_client, write_client = run_sweep(
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
    horizon_start, _ = sweep_horizon()
    still_visible = make_leg(
        source_event_id="e-yesterday",
        start=horizon_start + timedelta(hours=1),
        end=horizon_start + timedelta(hours=1, minutes=20),
    )
    read = FakeReadClient({CALENDAR_ID: [make_raw_block(still_visible, event_id="blk-today")]})

    payload, _read_client, write_client = run_sweep(
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


class _LeakyWriteClient(FakeWriteClient):
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
    payload, _read, _write = run_sweep(
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
    root = Path(__file__).resolve().parents[2]
    source = (root / "src/family/travel_blocks_write.py").read_text(encoding="utf-8")
    assert "str(exc)" not in source, (
        "a persisted `detail` must go through calendar_readonly.safe_error_detail — "
        "see #285; the raw text still goes to the log"
    )
    assert "safe_error_detail" in source
