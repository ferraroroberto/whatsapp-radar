"""Structural guard (#435): every direct, non-fetch resource-URL builder in the
webapp's static JS carries the bearer token.

An `<img>`/`Audio()` element can't set an `Authorization` header, so the token
has to ride as `?token=`/`&token=` on the URL itself — `chats.js`'s `audioUrl`
already does this; `execution.js`'s `qrSrc` didn't, and 401'd the phone
re-pairing QR over Tailscale once the bearer gate was on. Scoped to functions
named like a URL/src builder (`...Url`/`...Src`) that build a literal
`/api/...` path, not every `/api/` string in the tree — `fetch`/`jsonApi`/
`api()` call sites already carry the header and need no query-string token.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "webapp" / "static"

# `function xUrl(...) { ... }` / `function xSrc(...) { ... }` — matches to the
# function's own closing brace at column 0, good enough for this tree's flat,
# un-nested builder style.
_BUILDER = re.compile(r"function\s+(\w*(?:Url|Src))\s*\([^)]*\)\s*\{(.*?)\n\}", re.DOTALL)
_API_LITERAL = re.compile(r"['\"]/api/")


def _api_url_builders() -> list[tuple[str, str, Path]]:
    found = []
    for path in STATIC_DIR.rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        for match in _BUILDER.finditer(text):
            name, body = match.group(1), match.group(2)
            if _API_LITERAL.search(body):
                found.append((name, body, path))
    return found


def test_every_api_url_builder_carries_the_bearer_token() -> None:
    builders = _api_url_builders()
    # Guards the guard: if naming/structure drifts so the scan finds nothing,
    # that's a silent pass, not a clean bill of health.
    assert builders, (
        "expected at least one *Url/*Src builder referencing a literal /api/ "
        "path (e.g. chats.js's audioUrl) — update this test's scan if the "
        "naming convention changed"
    )
    missing = [f"{path.name}:{name}" for name, body, path in builders if "readToken(" not in body]
    assert not missing, (
        "these resource-URL builders construct a literal /api/ path with no "
        "readToken() call, so an <img>/Audio() request 401s once the bearer "
        f"token is set: {missing}"
    )
