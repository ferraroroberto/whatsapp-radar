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


def _static_with_vendored(tmp_path: Path) -> Path:
    vendored_nav = tmp_path / "_vendored" / "nav"
    vendored_nav.mkdir(parents=True)
    (vendored_nav / "nav-tabs.css").write_text(".nav{}", encoding="utf-8")
    vendored_icons = tmp_path / "_vendored" / "icons"
    vendored_icons.mkdir(parents=True)
    (vendored_icons / "icons.js").write_text("export const icon = () => {};\n", encoding="utf-8")
    vendored_empty_state = tmp_path / "_vendored" / "empty-state"
    vendored_empty_state.mkdir(parents=True)
    (vendored_empty_state / "empty-state.js").write_text(
        "import { icon } from '../icons/icons.js';\n", encoding="utf-8"
    )
    return tmp_path


def test_compute_hashes_keys_by_relpath_for_subdirectory_assets(tmp_path: Path) -> None:
    hashes = compute_asset_hashes(_static_with_vendored(tmp_path))
    assert set(hashes) == {
        "_vendored/nav/nav-tabs.css",
        "_vendored/icons/icons.js",
        "_vendored/empty-state/empty-state.js",
    }
    # Single fleet hash shared by every file, subdirectory ones included.
    assert len(set(hashes.values())) == 1


def test_compute_hashes_same_basename_in_different_dirs_does_not_collide(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "styles.css").write_text("body{color:red}", encoding="utf-8")
    (tmp_path / "b" / "styles.css").write_text("body{color:blue}", encoding="utf-8")
    hashes = compute_asset_hashes(tmp_path)
    # Both survive as distinct keys instead of one clobbering the other.
    assert set(hashes) == {"a/styles.css", "b/styles.css"}


def test_rewrite_index_html_stamps_vendored_subdirectory_css() -> None:
    hashes = {"_vendored/nav/nav-tabs.css": "cafebabe"}
    html = '<link rel="stylesheet" href="/static/_vendored/nav/nav-tabs.css">'
    out = rewrite_index_html(html, hashes)
    assert "/static/_vendored/nav/nav-tabs.css?v=cafebabe" in out


def test_rewrite_js_imports_stamps_subdirectory_import() -> None:
    hashes = {"_vendored/icons/icons.js": "abc123"}
    src = "import { icon } from './_vendored/icons/icons.js';"
    out = rewrite_js_imports(src, hashes, from_dir="")
    assert "./_vendored/icons/icons.js?v=abc123" in out


def test_rewrite_js_imports_resolves_parent_relative_import_from_subdir() -> None:
    hashes = {"_vendored/icons/icons.js": "abc123"}
    src = "import { icon } from '../icons/icons.js';"
    out = rewrite_js_imports(src, hashes, from_dir="_vendored/empty-state")
    assert "../icons/icons.js?v=abc123" in out


def test_rewrite_js_imports_root_level_still_stamps_without_from_dir() -> None:
    # No regression: a root-level file (from_dir defaults to "") still stamps
    # exactly as before this change.
    hashes = {"tabs.js": "abc123"}
    out = rewrite_js_imports("import { wireTabs } from './tabs.js';", hashes)
    assert "./tabs.js?v=abc123" in out
