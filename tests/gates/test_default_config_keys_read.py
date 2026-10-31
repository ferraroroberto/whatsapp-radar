"""Every top-level key in the committed `config/default.json` is read by `src/config`.

A block nothing reads is dead config that looks authoritative: the old `webapp`
block read as the home of the port while only `webapp_config.json` ever set it.
A quoted occurrence of the key in `src/config/*.py` is the structural proof that
`load_config` (or one of its domain parsers) consumes it.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_default_json_key_is_read_by_the_config_package() -> None:
    defaults = json.loads((REPO_ROOT / "config" / "default.json").read_text(encoding="utf-8"))
    source = "".join(
        path.read_text(encoding="utf-8") for path in (REPO_ROOT / "src" / "config").glob("*.py")
    )
    unread = sorted(key for key in defaults if f'"{key}"' not in source)
    assert not unread, f"config/default.json keys no src/config module reads: {unread}"
