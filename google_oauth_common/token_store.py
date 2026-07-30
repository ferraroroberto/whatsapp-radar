"""Atomic OAuth token persistence shared by the portable Google packages."""

from __future__ import annotations

from pathlib import Path


def write_token_atomically(path: Path, token_json: str) -> None:
    """Persist an OAuth token atomically without logging its contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(token_json, encoding="utf-8")
    temporary_path.replace(path)
