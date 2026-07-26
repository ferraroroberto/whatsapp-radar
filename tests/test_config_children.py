"""Config parsing for the household child registry (#206/#215)."""

from __future__ import annotations

import json
from pathlib import Path

from src.config import load_config


def test_children_default_empty(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.json").write_text(
        json.dumps({"db_path": "data/x.sqlite3"}), encoding="utf-8"
    )
    cfg = load_config(root=tmp_path)
    assert cfg.children == ()


def test_children_parse_from_local_json(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.json").write_text(
        json.dumps({"db_path": "data/x.sqlite3", "children": []}), encoding="utf-8"
    )
    (cfg_dir / "local.json").write_text(
        json.dumps(
            {
                "children": [
                    {"name": "Example Child A", "aliases": ["Ex"], "class_name": "4A"},
                    {"name": "Example Child B"},
                    {"name": "  "},  # blank name dropped
                ]
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(root=tmp_path)

    assert len(cfg.children) == 2
    assert cfg.children[0].name == "Example Child A"
    assert cfg.children[0].aliases == ("Ex",)
    assert cfg.children[0].class_name == "4A"
    assert cfg.children[1].name == "Example Child B"
    assert cfg.children[1].aliases == ()
    assert cfg.children[1].class_name == ""
