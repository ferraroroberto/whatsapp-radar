"""Cheap multilingual keyword prefilter for the cascade classifier.

Matches accent-stripped, lowercased message text against an inspectable list of
actionable ROOTS (``prompts/keyword_roots.txt``) covering Spanish, English, and
Catalan. This is a high-recall gate: its only job is to decide whether a message
delta is worth an LLM call, so it errs toward matching.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from importlib.resources import files

from ..models import StoredMessage

_ROOTS_FILE = "keyword_roots.txt"


@lru_cache(maxsize=1)
def load_keyword_roots() -> tuple[str, ...]:
    """Load the actionable roots, already normalized, ignoring comments/blanks."""
    text = (files("whatsapp_radar.analysis.prompts") / _ROOTS_FILE).read_text(encoding="utf-8")
    roots = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        roots.append(normalize(line))
    return tuple(roots)


def normalize(text: str) -> str:
    """Lowercase and strip accents so roots match across diacritics (café -> cafe)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def message_has_signal(text: str | None) -> bool:
    if not text:
        return False
    normalized = normalize(text)
    return any(root in normalized for root in load_keyword_roots())


def has_actionable_signal(delta: Iterable[StoredMessage]) -> bool:
    """True if any message in the delta contains an actionable root."""
    return any(message_has_signal(m.text) for m in delta)
