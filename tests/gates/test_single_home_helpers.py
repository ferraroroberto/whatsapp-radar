"""Two mechanisms have exactly one implementation, so they cannot drift apart.

Language detection (threshold, seed, failure handling) lives in
``src/language_detect.py``; additive column migrations of the main store go
through ``_add_missing_columns`` in ``src/db/connection.py``. A second copy of
either is a parallel implementation, so the scan fails on it.
"""

from __future__ import annotations

from src.paths import PROJECT_ROOT

_SRC = PROJECT_ROOT / "src"
_LANGUAGE_HOME = _SRC / "language_detect.py"
_STORE_CONNECTION = _SRC / "db" / "connection.py"


def _runtime_sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(PROJECT_ROOT).as_posix(), path.read_text(encoding="utf-8"))
        for directory in ("src", "app")
        for path in sorted((PROJECT_ROOT / directory).rglob("*.py"))
    ]


def test_langdetect_is_imported_only_by_the_shared_helper() -> None:
    home = _LANGUAGE_HOME.relative_to(PROJECT_ROOT).as_posix()
    offenders = [
        rel
        for rel, text in _runtime_sources()
        if rel != home and ("import langdetect" in text or "from langdetect" in text)
    ]
    assert not offenders, f"call src.language_detect.detect_language_code instead: {offenders}"


def test_store_columns_are_added_by_one_table_driven_helper() -> None:
    text = _STORE_CONNECTION.read_text(encoding="utf-8")
    assert text.count("ADD COLUMN") == 1, (
        "declare new columns in a *_ADDED_COLUMNS tuple and let _add_missing_columns apply them"
    )
