"""Content-hash stamping for /static assets.

The webapp ships ``index.html`` + a handful of ES-module ``.js`` files +
``styles.css``. iOS Safari (and especially the standalone PWA) caches those
aggressively, so a deploy isn't really "live" until the cached copies are
evicted. To make that deterministic we append ``?v=<hash>`` to every asset URL.
The hash is computed once at app startup from the content of the static dir;
tray restart on every code edit (project convention) means we don't need a
watcher.

We use a single **fleet hash** — sha256 over the concatenation of each file's
per-file hash, sorted by name. One value to log and to surface from
``/api/version`` for a visual diff against the deployed PC build. The asset
budget is tiny: any one edit re-downloads all hashed files on next visit, still
well under a second on LTE.

Functions are pure and easy to unit-test in isolation.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Iterable
from pathlib import Path

_HASH_LEN = 8

# Files under static/ that get hashed + long-cached. Everything else (icons,
# manifest) is cached more conservatively by the static mount.
_HASHED_SUFFIXES = (".js", ".css")

# Subdirectories under static/ to skip entirely (third-party vendor bundles are
# immutable per upstream version — their URLs never change).
_SKIP_DIRS = ("vendor",)

# ``import ... from './foo.js'`` or ``'../dir/foo.js'`` — captures the whole
# quoted relative specifier (any number of ``./``/``../`` segments and
# subdirectories), not just a bare root-level filename, so a vendored
# component's own relative import (e.g. ``'../icons/icons.js'``) can be
# stamped too.
_JS_IMPORT_RE = re.compile(
    r"""(from\s*['"])(\.\.?/(?:[\w\-.]+/)*[\w\-.]+\.js)(\?v=[^'"]*)?(['"])"""
)

# ``href``/``src`` pointing at a hashable ``/static/`` asset — including
# subdirectories (e.g. ``/static/_vendored/nav/nav-tabs.css``).
# ``/static/vendor/…`` still passes through unstamped because ``vendor``
# never appears in the hash map (``_SKIP_DIRS``), not because the regex
# excludes it.
_INDEX_ASSET_RE = re.compile(
    r"""(href|src)=(['"])/static/([\w\-./]+\.(?:css|js))(\?v=[^'"]*)?(['"])"""
)


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:_HASH_LEN]


def _iter_hashable_files(static_dir: Path) -> Iterable[Path]:
    for path in sorted(static_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(static_dir).parts[:-1]):
            continue
        if path.suffix.lower() not in _HASHED_SUFFIXES:
            continue
        yield path


def compute_asset_hashes(static_dir: Path) -> dict[str, str]:
    """Return ``{relpath: fleet_hash}`` for every hashable static file.

    Same hash for every file (the fleet hash). Keyed by the file's
    static-dir-relative posix path (e.g. ``_vendored/nav/nav-tabs.css``), not
    the bare filename, so subdirectory references resolve correctly and two
    files sharing a basename in different directories don't collide.
    ``fleet_hash`` is the sha256-over-sha256s described in the module
    docstring.
    """
    if not static_dir.exists():
        return {}
    per_file: dict[str, str] = {}
    for path in _iter_hashable_files(static_dir):
        relpath = path.relative_to(static_dir).as_posix()
        per_file[relpath] = _short_hash(path.read_bytes())
    if not per_file:
        return {}
    fleet_input = "\n".join(
        f"{name}:{per_file[name]}" for name in sorted(per_file)
    ).encode("utf-8")
    fleet_hash = _short_hash(fleet_input)
    return {name: fleet_hash for name in per_file}


def fleet_hash_of(hashes: dict[str, str]) -> str:
    """Single representative hash. Empty string if no assets."""
    if not hashes:
        return ""
    # By construction every value in ``hashes`` is the same fleet hash.
    return next(iter(hashes.values()))


def _resolve_specifier(from_dir: str, spec: str) -> str:
    """Resolve a ``./``/``../`` import specifier against ``from_dir``.

    ``from_dir`` is the static-dir-relative posix directory of the file doing
    the importing (empty string at the static root). Returns the
    static-dir-relative posix path used as the ``hashes`` lookup key, e.g.
    ``_resolve_specifier("_vendored/empty-state", "../icons/icons.js") ==
    "_vendored/icons/icons.js"``.
    """
    joined = posixpath.join(from_dir, spec) if from_dir else spec
    return posixpath.normpath(joined)


def rewrite_js_imports(body: str, hashes: dict[str, str], from_dir: str = "") -> str:
    """Stamp ``?v=<hash>`` onto every relative ``import`` in ``body``.

    ``from_dir`` is the static-dir-relative posix directory of the file being
    rewritten (empty string for a file at the static root) — needed to
    resolve ``./`` and ``../`` specifiers (including into subdirectories,
    e.g. ``./_vendored/icons/icons.js``) against ``hashes``, which is keyed
    by static-dir-relative path. Imports without a matching entry in
    ``hashes`` are left as-is — robust against new files not yet in the hash
    map. Existing ``?v=…`` is replaced so re-rewriting a server-stamped body
    is idempotent.
    """
    if not hashes:
        return body

    def _sub(match: re.Match[str]) -> str:
        prefix, spec, _existing, quote_close = match.group(1, 2, 3, 4)
        stamp = hashes.get(_resolve_specifier(from_dir, spec))
        if not stamp:
            return match.group(0)
        return f"{prefix}{spec}?v={stamp}{quote_close}"

    return _JS_IMPORT_RE.sub(_sub, body)


def rewrite_index_html(body: str, hashes: dict[str, str]) -> str:
    """Stamp ``?v=<hash>`` onto every ``/static/<relpath>.(css|js)`` href/src.

    ``<relpath>`` may include subdirectories (e.g.
    ``_vendored/nav/nav-tabs.css``) since it maps directly onto a ``hashes``
    key — no resolution needed, unlike a relative JS import. Same
    robustness rules as :func:`rewrite_js_imports` — unknown files pass
    through unchanged; existing version queries are replaced.
    """
    if not hashes:
        return body

    def _sub(match: re.Match[str]) -> str:
        attr, quote_open, relpath, _existing, quote_close = match.group(1, 2, 3, 4, 5)
        stamp = hashes.get(relpath)
        if not stamp:
            return match.group(0)
        return f"{attr}={quote_open}/static/{relpath}?v={stamp}{quote_close}"

    return _INDEX_ASSET_RE.sub(_sub, body)


def asset_hash_for(hashes: dict[str, str], name: str) -> str | None:
    """Lookup helper that survives an empty map without raising."""
    if not hashes:
        return None
    return hashes.get(name)
