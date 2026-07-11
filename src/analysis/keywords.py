"""Cheap source-aware keyword prefilter for the cascade classifier.

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

_ROOTS_FILES = {
    "whatsapp": "keyword_roots.txt",
    "gmail": "gmail_keyword_roots.txt",
}


def roots_file_path(source: str = "whatsapp") -> Path:
    """Absolute path to one source's inspectable keyword-root asset."""
    try:
        filename = _ROOTS_FILES[source]
    except KeyError as exc:
        raise ValueError(f"unsupported classification source: {source!r}") from exc
    return Path(__file__).with_name("prompts") / filename


@dataclass(frozen=True)
class KeywordRule:
    """One normalized root and the operator-facing bucket it belongs to."""

    bucket: str
    root: str


@dataclass(frozen=True)
class KeywordSignal:
    """Result of the Stage-1 prefilter: whether it matched and which roots did.

    ``matched`` is exposed via :meth:`__bool__` so the cascade can keep using
    ``if not has_actionable_signal(delta)``, while ``roots`` carries the Stage-1
    evidence the audit trace records.
    """

    matched: bool
    roots: tuple[str, ...] = ()
    buckets: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.matched


@lru_cache(maxsize=len(_ROOTS_FILES))
def load_keyword_rules(source: str = "whatsapp") -> tuple[KeywordRule, ...]:
    """Load normalized rules, ignoring comments and blank lines.

    WhatsApp's historical asset remains one root per line and is assigned to the
    generic ``actionable`` bucket. Gmail uses ``bucket | root`` so Audit can show
    both the deterministic category and the exact matching root.
    """
    text = roots_file_path(source).read_text(encoding="utf-8")
    rules: list[KeywordRule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if source == "gmail":
            bucket, separator, root = line.partition("|")
            if not separator or not bucket.strip() or not root.strip():
                raise ValueError("Gmail keyword rules must use 'bucket | root'")
        else:
            bucket, root = "actionable", line
        rules.append(KeywordRule(bucket=bucket.strip(), root=normalize(root.strip())))
    return tuple(rules)


def load_keyword_roots(source: str = "whatsapp") -> tuple[str, ...]:
    """Return only roots for compatibility with existing config surfaces."""
    return tuple(rule.root for rule in load_keyword_rules(source))


def normalize(text: str) -> str:
    """Lowercase and strip accents so roots match across diacritics (café -> cafe)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def matched_rules(text: str | None, source: str = "whatsapp") -> list[KeywordRule]:
    """Return every source-specific rule found in ``text``, in asset order."""
    if not text:
        return []
    normalized = normalize(text)
    return [rule for rule in load_keyword_rules(source) if rule.root in normalized]


def matched_roots(text: str | None, source: str = "whatsapp") -> list[str]:
    """Return every actionable root found in ``text`` (normalized), in roots order."""
    return [rule.root for rule in matched_rules(text, source)]


def message_has_signal(text: str | None, source: str = "whatsapp") -> bool:
    return bool(matched_rules(text, source))


def has_actionable_signal(
    delta: Iterable[StoredMessage], source: str = "whatsapp"
) -> KeywordSignal:
    """Stage-1 verdict over a delta, carrying the unique roots that matched.

    Truthy iff any message contains an actionable root; the matched roots are
    deduplicated while preserving first-seen order so the trace records evidence.
    """
    seen_roots: dict[str, None] = {}
    seen_buckets: dict[str, None] = {}
    for message in delta:
        for rule in matched_rules(message.text, source):
            seen_roots.setdefault(rule.root, None)
            seen_buckets.setdefault(rule.bucket, None)
    return KeywordSignal(
        matched=bool(seen_roots),
        roots=tuple(seen_roots),
        buckets=tuple(seen_buckets),
    )
