"""``runs.list_runs`` answers an unchanged runs folder from stat calls alone.

The Activity poll lists runs every 1.5 s while a run is in flight; re-reading
every ``run.json`` on each call is what the cache removes. These tests count the
real file reads, and pin that every kind of change still shows up.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.webapp import runs


@pytest.fixture
def runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "runs"
    monkeypatch.setattr(runs, "RUNS_DIR", target)
    return target


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Path]]:
    """Every ``run.json`` the code opens for reading, in order."""
    seen: list[Path] = []
    real = Path.read_text

    def counting(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "run.json":
            seen.append(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    yield seen


def _seed(kind: str, run_id: str) -> Path:
    run_dir = runs.new_run_dir(kind, run_id)
    runs.write_run_json(run_dir, status="completed", started_at=run_id)
    return run_dir


def test_an_unchanged_folder_costs_no_reads(runs_dir: Path, reads: list[Path]) -> None:
    for i in range(6):
        _seed("scan", f"2026010{i + 1}T000000")
        _seed("resync", f"2026020{i + 1}T000000")
    reads.clear()

    first = runs.list_runs(50)
    assert len(reads) == 12
    reads.clear()
    second = runs.list_runs(50)
    third = runs.list_runs(50)

    assert reads == []
    assert second == first == third


def test_a_rewritten_record_is_read_again(runs_dir: Path, reads: list[Path]) -> None:
    run_dir = _seed("scan", "20260101T000000")
    _seed("scan", "20260102T000000")
    assert {r["run_id"]: r["status"] for r in runs.list_runs()}["20260101T000000"] == "completed"

    runs.write_run_json(run_dir, status="failed", exit_code=1)  # reads run.json to merge
    reads.clear()
    listed = {r["run_id"]: r for r in runs.list_runs()}

    assert listed["20260101T000000"]["status"] == "failed"
    assert reads == [run_dir / "run.json"]


def test_a_new_and_a_removed_run_show_up(runs_dir: Path) -> None:
    old = _seed("scan", "20260101T000000")
    assert [r["run_id"] for r in runs.list_runs()] == ["20260101T000000"]

    _seed("scan", "20260102T000000")
    assert [r["run_id"] for r in runs.list_runs()] == ["20260102T000000", "20260101T000000"]

    for child in old.iterdir():
        child.unlink()
    old.rmdir()
    assert [r["run_id"] for r in runs.list_runs()] == ["20260102T000000"]


def test_a_new_kind_directory_and_a_vanished_runs_folder(runs_dir: Path) -> None:
    _seed("scan", "20260101T000000")
    assert len(runs.list_runs()) == 1
    _seed("traffic-check", "20260103T000000")
    assert [r["kind"] for r in runs.list_runs()] == ["traffic-check", "scan"]

    for path in sorted(runs_dir.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        else:
            path.rmdir()
    runs_dir.rmdir()
    assert runs.list_runs() == []


def test_the_caller_may_mutate_a_listed_record(runs_dir: Path) -> None:
    _seed("scan", "20260101T000000")
    runs.list_runs()[0]["mode"] = "dry_run"
    runs.list_runs()[0].pop("status")

    again = runs.list_runs()[0]
    assert "mode" not in again
    assert again["status"] == "completed"


def test_a_record_without_run_json_lists_by_name(runs_dir: Path) -> None:
    (runs_dir / "scan" / "20260101T000000").mkdir(parents=True)

    assert runs.list_runs() == [{"kind": "scan", "run_id": "20260101T000000"}]
