"""Cheap multilingual keyword prefilter for the cascade classifier.

Matches accent-stripped, lowercased message text against an inspectable list of
actionable ROOTS (``prompts/keyword_roots.txt``) covering Spanish, English, and
Catalan. This is a high-recall gate: its only job is to decide whether a message
delta is worth an LLM call, so it errs toward matching.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.models import StoredMessage

_ROOTS_FILE = "keyword_roots.txt"


def roots_file_path() -> Path:
    """Absolute path to the keyword-roots file (the Config tab shows it read-only)."""
    return Path(__file__).with_name("prompts") / _ROOTS_FILE


@dataclass(frozen=True)
class KeywordSignal:
    """Result of the Stage-1 prefilter: whether it matched and which roots did.

    ``matched`` is exposed via :meth:`__bool__` so the cascade can keep using
    ``if not has_actionable_signal(delta)``, while ``roots`` carries the Stage-1
    evidence the audit trace records.
    """

    matched: bool
    roots: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.matched


@lru_cache(maxsize=1)
def load_keyword_roots() -> tuple[str, ...]:
    """Load the actionable roots, already normalized, ignoring comments/blanks."""
    text = roots_file_path().read_text(encoding="utf-8")
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


def matched_roots(text: str | None) -> list[str]:
    """Return every actionable root found in ``text`` (normalized), in roots order."""
    if not text:
        return []
    normalized = normalize(text)
    return [root for root in load_keyword_roots() if root in normalized]


def message_has_signal(text: str | None) -> bool:
    return bool(matched_roots(text))


def has_actionable_signal(delta: Iterable[StoredMessage]) -> KeywordSignal:
    """Stage-1 verdict over a delta, carrying the unique roots that matched.

    Truthy iff any message contains an actionable root; the matched roots are
    deduplicated while preserving first-seen order so the trace records evidence.
    """
    seen: dict[str, None] = {}
    for message in delta:
        for root in matched_roots(message.text):
            seen.setdefault(root, None)
    return KeywordSignal(matched=bool(seen), roots=tuple(seen))
