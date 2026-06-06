"""Asset-hash stamping: pure functions, no FastAPI needed."""

from __future__ import annotations

from pathlib import Path

from src.static_versioning import (
    asset_hash_for,
    compute_asset_hashes,
    fleet_hash_of,
    rewrite_index_html,
    rewrite_js_imports,
)


def _static(tmp_path: Path) -> Path:
    (tmp_path / "styles.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "main.js").write_text("import './tabs.js';\n", encoding="utf-8")
    (tmp_path / "tabs.js").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "icon.png").write_bytes(b"\x89PNG")  # not hashed
    return tmp_path


def test_compute_hashes_covers_only_js_and_css(tmp_path: Path) -> None:
    hashes = compute_asset_hashes(_static(tmp_path))
    assert set(hashes) == {"styles.css", "main.js", "tabs.js"}
    # Single fleet hash shared by every file.
    assert len(set(hashes.values())) == 1
    assert fleet_hash_of(hashes) == next(iter(hashes.values()))


def test_compute_hashes_changes_when_a_file_changes(tmp_path: Path) -> None:
    before = compute_asset_hashes(_static(tmp_path))
    (tmp_path / "styles.css").write_text("body{color:red}", encoding="utf-8")
    after = compute_asset_hashes(tmp_path)
    assert before["styles.css"] != after["styles.css"]


def test_empty_dir_yields_empty_map(tmp_path: Path) -> None:
    assert compute_asset_hashes(tmp_path) == {}
    assert fleet_hash_of({}) == ""
    assert asset_hash_for({}, "styles.css") is None


def test_rewrite_js_imports_stamps_known_files() -> None:
    hashes = {"tabs.js": "abc123"}
    src = "import { wireTabs } from './tabs.js';\nimport { x } from './unknown.js';"
    out = rewrite_js_imports(src, hashes)
    assert "./tabs.js?v=abc123" in out
    assert "./unknown.js'" in out  # unknown left untouched


def test_rewrite_js_imports_is_idempotent() -> None:
    hashes = {"tabs.js": "abc123"}
    once = rewrite_js_imports("import { wireTabs } from './tabs.js';", hashes)
    twice = rewrite_js_imports(once, hashes)
    assert once == twice


def test_rewrite_index_html_stamps_css_and_js() -> None:
    hashes = {"styles.css": "deadbeef", "main.js": "deadbeef"}
    html = '<link href="/static/styles.css"><script src="/static/main.js"></script>'
    out = rewrite_index_html(html, hashes)
    assert "/static/styles.css?v=deadbeef" in out
    assert "/static/main.js?v=deadbeef" in out
