"""The atomic writer swaps new content in; a failed write leaves the old file and no temp."""

from __future__ import annotations

from pathlib import Path

import pytest

from src import atomic_write
from src.atomic_write import atomic_write_text


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """A folder of its own, so the listing sees only what the writer left."""
    path = tmp_path / "out"
    path.mkdir()
    return path


def test_a_replace_lands_the_new_content(folder: Path) -> None:
    target = folder / "run.json"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, '{"k": "ü"}')
    assert target.read_text(encoding="utf-8") == '{"k": "ü"}'
    assert [p.name for p in folder.iterdir()] == ["run.json"]


def test_a_failed_replace_keeps_the_old_content_and_no_temp(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = folder / "local.json"
    target.write_text("old", encoding="utf-8")

    def locked(src: str, dst: Path) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(atomic_write.os, "replace", locked)
    with pytest.raises(PermissionError):
        atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in folder.iterdir()] == ["local.json"]


def test_a_failed_write_keeps_the_old_content_and_no_temp(folder: Path) -> None:
    target = folder / "devices.json"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        atomic_write_text(target, "\udcff")  # a lone surrogate cannot be encoded
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in folder.iterdir()] == ["devices.json"]
