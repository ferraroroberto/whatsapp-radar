"""save_local_overrides deep-merges into the gitignored config/local.json.

This is the host-override writer the Config tab's safe settings persist through.
It must merge (never clobber sibling keys) and survive an absent file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import config


def _make_root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    return tmp_path


def test_creates_file_when_absent(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    config.save_local_overrides({"connector": "linked_device"}, root=root)
    written = json.loads((root / "config" / "local.json").read_text(encoding="utf-8"))
    assert written == {"connector": "linked_device"}


def test_deep_merges_without_clobbering(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    local = root / "config" / "local.json"
    local.write_text(
        json.dumps({"connector": "fixture", "hub": {"base_url": "http://x", "model": "old"}}),
        encoding="utf-8",
    )
    config.save_local_overrides({"classifier": "cascade", "hub": {"model": "new"}}, root=root)
    written = json.loads(local.read_text(encoding="utf-8"))
    assert written == {
        "connector": "fixture",          # untouched
        "classifier": "cascade",         # added
        "hub": {"base_url": "http://x", "model": "new"},  # nested-merged
    }


def test_load_config_reads_local_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # load_config layers WR_* env over the file values; a leaked WR_CONNECTOR from
    # the developer's real .env (read into os.environ by an earlier load_config)
    # would otherwise mask the default — isolate it so the merge is what's tested.
    monkeypatch.delenv("WR_CONNECTOR", raising=False)
    monkeypatch.delenv("WR_CLASSIFIER", raising=False)
    root = _make_root(tmp_path)
    (root / "config" / "default.json").write_text(
        json.dumps({"connector": "fixture", "classifier": "stub"}), encoding="utf-8"
    )
    config.save_local_overrides({"classifier": "cascade"}, root=root)
    cfg = config.load_config(root=root)
    assert cfg.classifier == "cascade"
    assert cfg.connector == "fixture"
