"""The travel-block plan has one renderer: the log and the CLI print the same lines."""

from __future__ import annotations

import logging

import pytest

from src.family import travel_blocks
from src.family.travel_blocks_report import format_travel_blocks_progress, plan_lines
from tests.fakes.travel_block_doubles import make_block, make_leg


def _plan() -> travel_blocks.TravelBlockPlan:
    added = make_leg(minutes=25)
    stale = make_block(make_leg(source_event_id="e2"), event_id="blk-stale")
    spared = make_block(make_leg(source_event_id="e3"), event_id="blk-spared")
    return travel_blocks.TravelBlockPlan(
        status=travel_blocks.STATUS_OK,
        dry_run=True,
        legs=[added],
        adds=[added],
        deletes=[travel_blocks.PlannedDelete(stale, travel_blocks.DELETE_REASON_ORPHANED)],
        failures=[
            travel_blocks.LegFailure(
                "dad", "cal", "e4", "return", travel_blocks.FAILURE_ROUTES_ERROR, "routes down"
            )
        ],
        routes_calls=2,
        protected=[
            travel_blocks.ProtectedBlock(spared, travel_blocks.PROTECT_REASON_UNPLANNED_LEG)
        ],
    )


def test_the_log_and_the_cli_print_the_same_lines(caplog: pytest.LogCaptureFixture) -> None:
    plan = _plan()
    with caplog.at_level(logging.INFO, logger=travel_blocks.logger.name):
        travel_blocks.log_plan(plan)
    logged = [record.getMessage() for record in caplog.records]
    assert logged == format_travel_blocks_progress(plan.to_payload())


def test_a_block_left_alone_is_named_with_the_fact_that_spared_it() -> None:
    lines = format_travel_blocks_progress(_plan().to_payload())
    spared = next(line for line in lines if "left alone (leg_not_planned)" in line)
    assert "blk-spared" in spared
    assert "never applied as a delete" in spared
    assert any("1 left alone" in line for line in lines)


def test_only_the_unpriced_leg_lines_log_at_warning() -> None:
    warnings = [line for line in plan_lines(_plan().to_payload()) if line.level == logging.WARNING]
    assert len(warnings) == 1
    assert "unpriced" in warnings[0].text


def test_a_disabled_sweep_is_quiet_on_the_cli_but_still_a_log_line() -> None:
    section = {"status": travel_blocks.STATUS_DISABLED}
    assert format_travel_blocks_progress(section) == []
    assert "disabled" in plan_lines(section)[0].text


def test_a_misconfigured_sweep_stays_loud_on_the_cli() -> None:
    lines = format_travel_blocks_progress({"status": travel_blocks.STATUS_NO_ROUTES_API_KEY})
    assert len(lines) == 1
    assert "no_routes_api_key" in lines[0]
