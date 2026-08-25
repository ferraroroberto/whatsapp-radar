"""The priced/unpriced leg vocabulary and its four readers (#283).

A leg that was never priced — `anchor_in_the_past` (the drive is over, no Routes
call spent) or `error` (the call failed) — established nothing about the road.
Counting it as *checked* makes a run in which nothing was priced report a
healthy-looking coverage number: real, plausible and wrong, and the reason a
check can quietly stop running without anybody noticing.

#270 fixed the worst instance (the Dashboard card said "no significant delay").
These tests cover the three sibling surfaces it deferred, plus the one property
that makes a single shared vocabulary possible at all: importing it costs the
webapp nothing.

Offline throughout — no Routes call, no Google client, no network. The statuses
under test are produced without a Routes call by construction.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.cli import main as cli
from app.webapp.routers import family as family_router
from src.family import leg_status, traffic_check
from src.family.leg_status import (
    STATUS_ANCHOR_IN_THE_PAST,
    STATUS_ERROR,
    UNPRICED_LEG_STATUSES,
    count_legs,
)
from src.subprocess_flags import NO_WINDOW

# --------------------------------------------------------------- the vocabulary


def test_the_two_unpriced_statuses_are_exactly_the_ones_that_price_nothing() -> None:
    assert UNPRICED_LEG_STATUSES == {"anchor_in_the_past", "error"}
    assert STATUS_ANCHOR_IN_THE_PAST == "anchor_in_the_past"
    assert STATUS_ERROR == "error"


def test_traffic_check_reports_the_same_status_string_this_module_defines() -> None:
    """The producer and the counters must not be able to drift apart (#283).

    `traffic_check` is where the status is written into the payload; four other
    places read it back. If it stopped being literally the same object the
    counts would go quietly wrong rather than loudly.
    """
    assert traffic_check.STATUS_ANCHOR_IN_THE_PAST is STATUS_ANCHOR_IN_THE_PAST


@pytest.mark.parametrize(
    ("legs", "expected"),
    [
        (None, (0, 0)),
        ([], (0, 0)),
        ([{"status": "ok"}, {"status": "ok"}], (2, 0)),
        ([{"status": STATUS_ANCHOR_IN_THE_PAST}], (0, 1)),
        ([{"status": STATUS_ERROR}], (0, 1)),
        ([{"status": "ok"}, {"status": STATUS_ERROR}], (1, 1)),
        ([{"status": STATUS_ERROR}, {"status": STATUS_ANCHOR_IN_THE_PAST}], (0, 2)),
        # A payload from before #270 carries no `status` at all, and every one of
        # its legs genuinely was priced. Counting those as unpriced would invent
        # a coverage gap that never existed.
        ([{"person": "parent-a"}], (1, 0)),
        (["not-a-mapping"], (1, 0)),
    ],
)
def test_count_legs_splits_priced_from_unpriced(
    legs: Any, expected: tuple[int, int]
) -> None:
    assert count_legs(legs) == expected


def test_importing_the_vocabulary_pulls_in_no_google_client() -> None:
    """The property that lets the webapp share one spelling instead of copying it.

    `dashboard.py` originally spelled the statuses as literals precisely because
    importing `src.family.traffic_check` drags the Google client libraries into
    webapp startup through `calendar_source`. A clean subprocess is the only
    honest way to assert the replacement does not: inside this test session
    those modules are already imported by everything else.
    """
    probe = (
        "import sys; import src.family.leg_status;"
        "print(sorted(m for m in sys.modules"
        " if m.split('.')[0] in {'google', 'googleapiclient', 'google_auth_oauthlib',"
        " 'calendar_readonly', 'requests'}))"
    )
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, cwd=root, check=True,
        creationflags=NO_WINDOW,
    )
    assert proc.stdout.strip() == "[]", proc.stdout


# --------------------------------------------------------------- surface 1: the CLI


def _run_traffic_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> int:
    monkeypatch.setenv("WR_DB_PATH", str(tmp_path / "family.sqlite3"))

    def fake_runner(config: Any, *, now: Any, dry_run: bool) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(traffic_check, "run_traffic_check", fake_runner)
    return cli.main(["traffic-check", "--dry-run"])


def _payload(*statuses: str) -> dict[str, Any]:
    return {
        "kind": "traffic-check",
        "status": "ok",
        "alerts": 0,
        "checked": [{"person": "parent-a", "status": s} for s in statuses],
    }


def test_cli_never_counts_an_unpriced_leg_as_a_priced_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run where nothing was priced must not print a healthy route count."""
    payload = _payload(STATUS_ERROR, STATUS_ANCHOR_IN_THE_PAST)
    assert _run_traffic_cli(tmp_path, monkeypatch, payload) == 0

    line = next(
        ln for ln in capsys.readouterr().out.splitlines() if "traffic-check:" in ln
    )
    assert "0 route(s) priced" in line
    assert "2 not priced" in line
    # The old wording claimed coverage this run does not have.
    assert "2 route(s) checked" not in line


def test_cli_names_the_unpriced_half_of_a_mixed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Some judged, some not — the granularity #270 had to fix on the Dashboard."""
    assert _run_traffic_cli(tmp_path, monkeypatch, _payload("ok", "ok", STATUS_ERROR)) == 0

    line = next(
        ln for ln in capsys.readouterr().out.splitlines() if "traffic-check:" in ln
    )
    assert "2 route(s) priced" in line
    assert "1 not priced" in line


def test_cli_stays_quiet_about_unpriced_legs_when_there_are_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fully-priced run reads exactly as before — no new noise on the happy path."""
    assert _run_traffic_cli(tmp_path, monkeypatch, _payload("ok", "ok")) == 0

    line = next(
        ln for ln in capsys.readouterr().out.splitlines() if "traffic-check:" in ln
    )
    assert "2 route(s) priced" in line
    assert "not priced" not in line


# --------------------------------------------------------------- surface 2: /api/family


class _Row(dict):  # type: ignore[type-arg]
    """A stand-in for the `sqlite3.Row` mapping `_run_summary` reads."""

    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _run_row(*statuses: str) -> _Row:
    return _Row(
        id=1, kind="traffic-check", status="completed", mode="dry_run",
        started_at="2026-07-20T07:00:00+00:00", completed_at="2026-07-20T07:00:05+00:00",
        summary_json=json.dumps(_payload(*statuses)),
    )


def test_the_family_run_summary_reports_the_split_not_just_a_total() -> None:
    summary = family_router._run_summary(_run_row("ok", STATUS_ERROR, STATUS_ANCHOR_IN_THE_PAST))

    assert summary["priced"] == 1
    assert summary["unpriced"] == 2
    # The old field stays, so a client reading it is not left with nothing —
    # it is simply no longer the only number available.
    assert summary["checked"] == 3


def test_an_all_unpriced_family_run_reports_zero_priced() -> None:
    """The criterion in as many words: never a non-zero count without the truth."""
    summary = family_router._run_summary(_run_row(STATUS_ERROR, STATUS_ANCHOR_IN_THE_PAST))

    assert summary["priced"] == 0
    assert summary["unpriced"] == 2


def test_a_legacy_run_row_without_statuses_still_reads_as_fully_priced() -> None:
    """Rows written before #270 carry no `status`, and were genuinely all priced."""
    row = _Row(
        id=2, kind="traffic-check", status="completed", mode="live",
        started_at="2026-01-01T07:00:00+00:00", completed_at="2026-01-01T07:00:05+00:00",
        summary_json=json.dumps({
            "kind": "traffic-check", "status": "ok", "alerts": 1,
            "checked": [{"person": "parent-a"}, {"person": "parent-b"}],
        }),
    )

    summary = family_router._run_summary(row)

    assert (summary["priced"], summary["unpriced"], summary["checked"]) == (2, 0, 2)


# --------------------------------------------------------------- surface 3: the JS spelling


def test_the_javascript_spelling_matches_the_python_one() -> None:
    """Two languages, two spellings — but they must name the same statuses (#283).

    The browser cannot import `leg_status.py`, so `format.js` carries the one
    JavaScript copy. Asserted on the source because the guarantee is "these two
    lists agree", which no amount of DOM stubbing from Python can demonstrate.
    """
    root = Path(__file__).resolve().parents[1]
    fmt = (root / "app/webapp/static/format.js").read_text(encoding="utf-8")
    line = next(
        ln for ln in fmt.splitlines() if ln.startswith("const UNPRICED_LEG_STATUSES")
    )
    spelled = {chunk.strip().strip("'\"") for chunk in line.split("[")[1].split("]")[0].split(",")}
    assert spelled == set(UNPRICED_LEG_STATUSES)

    # And there is exactly one such list in the browser bundle — the whole point.
    static = root / "app/webapp/static"
    others = [
        path.name
        for path in sorted(static.glob("*.js"))
        if path.name != "format.js" and "anchor_in_the_past" in path.read_text(encoding="utf-8")
    ]
    assert others == [], f"the unpriced vocabulary is spelled again in {others}"


#: `travel_blocks.FAILURE_ANCHOR_IN_THE_PAST` is a *different* vocabulary that
#: happens to agree on one string: it is a travel-block sweep's failure reason,
#: not a traffic-check leg's status, and it rides a different payload. The two
#: were written to mirror each other deliberately, and `traffic_check`'s own
#: docstring says "mirrors", not "is". Collapsing them would couple two
#: independent vocabularies on a coincidence, so it keeps its own spelling — and
#: is named here rather than being quietly skipped by a looser pattern.
_NOT_THIS_VOCABULARY = {"src/family/travel_blocks.py"}


def test_no_python_surface_spells_the_leg_statuses_by_hand_any_more() -> None:
    """The criterion: not a fourth hand-written copy anywhere (#283)."""
    root = Path(__file__).resolve().parents[1]
    owner = "src/family/leg_status.py"
    offenders = []
    for path in sorted([*(root / "src").rglob("*.py"), *(root / "app").rglob("*.py")]):
        relative = path.relative_to(root).as_posix()
        if relative == owner or relative in _NOT_THIS_VOCABULARY:
            continue
        if '"anchor_in_the_past"' in path.read_text(encoding="utf-8"):
            offenders.append(relative)
    assert offenders == [], f"unpriced leg statuses re-spelled in {offenders}"


def test_the_dashboard_no_longer_keeps_its_own_copy_of_the_set() -> None:
    """#270 left a local frozenset with a comment explaining why (#283 removes it).

    That comment was right at the time — importing `traffic_check` really would
    drag the Google clients into webapp startup. The fix is a leaf module that
    does not, not four copies of a literal.
    """
    root = Path(__file__).resolve().parents[1]
    dashboard = (root / "app/webapp/routers/dashboard.py").read_text(encoding="utf-8")
    assert "_UNPRICED_LEG_STATUSES" not in dashboard
    assert "from src.family.leg_status import" in dashboard


def test_the_module_is_a_leaf_and_must_stay_one() -> None:
    """Its whole value is that importing it is free; that is easy to lose."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "src/family/leg_status.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            assert not line.startswith(("import src", "from src", "import app", "from app")), line
    assert leg_status.__doc__ is not None
