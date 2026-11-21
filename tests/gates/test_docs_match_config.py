"""Docs naming the enabled ratchet rules or a tab label must match the shipped config and app.

The conventions-ratchet section of `docs/verification.md` is prose about a list that lives in
`.fleet.toml`, and the PWA tab labels live in `index.html`; neither is read by pytest/ruff/mypy,
so a rule added to the config or a tab renamed leaves the docs describing something else.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _doc_files() -> list[Path]:
    return [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("**/*.md"))]


def test_verification_doc_names_every_enabled_conventions_rule() -> None:
    config = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    ratchet = config["conventions_ratchet"]
    section = (REPO_ROOT / "docs" / "verification.md").read_text(encoding="utf-8")
    missing = [f"`{rule}`" for rule in ratchet["rules"] if f"`{rule}`" not in section]
    for entry in ratchet.get("must_use", []):
        helpers = [entry["helper"]] if isinstance(entry["helper"], str) else entry["helper"]
        missing += [helper for helper in helpers if helper not in section]
    assert not missing, f"docs/verification.md does not name: {missing}"


def test_docs_name_the_inbox_tab_not_messages() -> None:
    html = (REPO_ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
    assert '<span class="tab-label">Inbox</span>' in html
    stale = re.compile(r"\*\*Messages\*\* tab|Messages tab|Activity, Messages,")
    hits = [doc.name for doc in _doc_files() if stale.search(doc.read_text(encoding="utf-8"))]
    assert not hits, f"docs call the Inbox tab 'Messages': {hits}"
