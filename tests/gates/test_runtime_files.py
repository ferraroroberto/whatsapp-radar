"""The runtime writers refuse a path under a code folder and write anywhere else."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src import runtime_files


def test_checked_refuses_each_code_folder() -> None:
    for name in runtime_files.CODE_FOLDERS:
        with pytest.raises(ValueError, match="code folder"):
            runtime_files.checked(runtime_files.REPO_ROOT / name / "state.txt")


def test_checked_refuses_the_folder_itself() -> None:
    with pytest.raises(ValueError):
        runtime_files.checked(runtime_files.REPO_ROOT / "src")


def test_checked_allows_gitignored_live_state_inside_the_checkout() -> None:
    for name in ("webapp", "config", "data"):
        target = runtime_files.REPO_ROOT / name / "x.log"
        assert runtime_files.checked(target) == target.resolve()


def test_refused_path_writes_nothing() -> None:
    target = runtime_files.REPO_ROOT / "src" / "_never_written" / "x.txt"
    with pytest.raises(ValueError):
        runtime_files.make_dirs(target.parent)
    with pytest.raises(ValueError):
        runtime_files.save_text(target, "x")
    assert not target.parent.exists()


def test_writers_work_outside_the_code_folders(tmp_path: Path) -> None:
    folder = runtime_files.make_dirs(tmp_path / "a" / "b")
    assert folder.is_dir()

    runtime_files.save_text(folder / "note.txt", "hello")
    assert (folder / "note.txt").read_text(encoding="utf-8") == "hello"

    with runtime_files.open_for_write(folder / "note.txt", "a", encoding="utf-8") as fh:
        fh.write(" world")
    assert (folder / "note.txt").read_text(encoding="utf-8") == "hello world"

    copy = runtime_files.copy_file(folder / "note.txt", folder / "copy.txt")
    assert copy.read_text(encoding="utf-8") == "hello world"


def test_make_dirs_can_claim_a_folder_exclusively(tmp_path: Path) -> None:
    runtime_files.make_dirs(tmp_path / "run", parents=False, exist_ok=False)
    with pytest.raises(FileExistsError):
        runtime_files.make_dirs(tmp_path / "run", parents=False, exist_ok=False)


def test_log_handlers_open_their_file_and_refuse_code_folders(tmp_path: Path) -> None:
    plain = runtime_files.file_handler(tmp_path / "plain.log")
    rotating = runtime_files.rotating_file_handler(
        tmp_path / "rot.log", maxBytes=1000, backupCount=1, encoding="utf-8", delay=True
    )
    try:
        assert isinstance(plain, logging.FileHandler)
        assert Path(rotating.baseFilename) == (tmp_path / "rot.log").resolve()
    finally:
        plain.close()
        rotating.close()
    with pytest.raises(ValueError):
        runtime_files.file_handler(runtime_files.REPO_ROOT / "app" / "x.log")
    with pytest.raises(ValueError):
        runtime_files.rotating_file_handler(runtime_files.REPO_ROOT / "scripts" / "x.log")
