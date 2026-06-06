"""Inspectable prompt assets for classification.

The classification prompt lives here as a plain Markdown file rather than a
Python string literal so it can be read, reviewed, and edited without touching
code. :func:`load_prompt` reads a named ``.md`` file from this directory verbatim.
"""

from __future__ import annotations

from pathlib import Path


def load_prompt(name: str) -> str:
    """Return the verbatim text of ``<name>.md`` shipped alongside this module."""
    return Path(__file__).with_name(f"{name}.md").read_text(encoding="utf-8").strip()
