"""The Audit dump's whitelist must stay complete as the payload evolves (#285).

`app/webapp/static/redact.js` withholds every payload key it does not
explicitly recognise. That is the right default — a field nobody has thought
about yet is redacted rather than exposed — but it has a failure mode of its
own: a genuinely diagnostic field added later would be silently withheld
forever, and the dump would quietly rot into uselessness while still *looking*
complete.

So the whitelist is pinned against the payload builders themselves. Every string
key those modules construct must be classified: renderable, deliberately
withheld, or never part of a run payload at all. A new key fails this test until
somebody decides which it is — which is the point. The browser-side proof that
nothing sensitive actually reaches the DOM lives in
`tests/e2e/test_audit_payload_redaction.py`, planted-sentinel style.

Offline and static: this reads source, runs nothing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REDACT_JS = ROOT / "app/webapp/static/redact.js"

#: The modules that build a family run payload. Deliberately only these: the
#: `app/` modules that touch the payload also build the whole `/api/family`
#: response and the CLI's own output, so harvesting them wholesale would drag in
#: ~55 keys that never reach `summary_json` and drown the signal this test
#: exists for.
_PAYLOAD_MODULES = (
    "src/family/traffic_check.py",
    "src/family/calendar_scan.py",
    "src/family/travel_blocks.py",
    "src/family/travel_blocks_write.py",
    "src/family/rules.py",
)

#: Keys stamped onto the payload *after* those modules built it, so they carry
#: no source there. Named rather than left to look like whitelist entries with
#: no origin — that mistaken reading is how a real field gets pruned.
#:   run_id            `app/cli/main.py` — `payload["run_id"] = run_id`
#:   priced, unpriced  `app/webapp/routers/family.py` — the #283 split
#:   unknown_locations the pre-#168 name for `missing_locations`; old rows persist
#:   duplicate_calendars, label   the #273 collapse warning, by label never by id
_ADDED_DOWNSTREAM = {
    "run_id", "priced", "unpriced", "unknown_locations",
    "duplicate_calendars", "label",
}

#: Withheld on purpose, each because it *is* the sensitive value — a real email
#: address or a literal street address in this household's configuration. This
#: set is the security contract; a test below asserts none of them is renderable.
_WITHHELD_ON_PURPOSE = {
    "calendar_id",     # a real email address
    "origin",          # a literal street address, or the live-fix label for one
    "origin_label",    # the same value, before it is copied to `origin`
    "origin_latlng",   # a raw GPS fix — more precise than an address
    "destination",     # a literal street address
    "raw_location",    # the source event's own location string
    "location",        # a travel block's `location` field, i.e. the destination
}

#: Keys these modules construct for the **Google Calendar event resource**, not
#: for a run payload. `build_block_event` builds the thing we POST to Google;
#: none of it is persisted as `summary_json`. Listed rather than filtered by
#: function name so that if one ever *does* reach the payload, it arrives
#: withheld-by-default and this list is where the decision gets recorded.
_NOT_IN_THE_RUN_PAYLOAD = {
    "dateTime", "extendedProperties", "private", "visibility",
    "transparency", "reminders", "useDefault",
}


def _js_set(name: str) -> set[str]:
    """The contents of a `const <name> = new Set([...])` literal in redact.js."""
    source = REDACT_JS.read_text(encoding="utf-8")
    match = re.search(rf"const {name} = new Set\(\[(.*?)\]\);", source, re.S)
    assert match, f"{name} not found in redact.js"
    # Strip line comments, then read the quoted entries.
    body = re.sub(r"//[^\n]*", "", match.group(1))
    return set(re.findall(r"'([^']+)'", body))


def _payload_keys() -> set[str]:
    """Every string key the payload builders construct.

    Both shapes, because both are used and only one of them is obvious:
    a dict literal (`{"status": ...}`), and a subscript assignment onto an
    already-built payload (`payload["apply"] = ...`, `payload["run_id"] = ...`).
    An earlier cut of this harvest read only the literals and therefore reported
    `apply` and `run_id` as whitelist entries with no source — the opposite of
    the truth, and exactly the kind of confidently-wrong finding that gets a
    real field pruned from the whitelist.
    """
    keys: set[str] = set()
    for relative in _PAYLOAD_MODULES:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys.update(
                    key.value
                    for key in node.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                if isinstance(node.slice.value, str):
                    keys.add(node.slice.value)
    return keys


def test_every_payload_key_is_classified() -> None:
    """A new payload field must be a decision, never a silent omission."""
    renderable = _js_set("RENDERABLE_KEYS")
    unclassified = (
        _payload_keys() - renderable - _WITHHELD_ON_PURPOSE - _NOT_IN_THE_RUN_PAYLOAD
    )

    assert unclassified == set(), (
        "these payload keys are neither on redact.js's whitelist nor recorded as "
        f"deliberately withheld, so the Audit dump silently hides them: {sorted(unclassified)}"
    )


@pytest.mark.parametrize("key", sorted(_WITHHELD_ON_PURPOSE))
def test_a_sensitive_key_is_never_renderable(key: str) -> None:
    """The security half, asserted key by key so a failure names the leak."""
    assert key not in _js_set("RENDERABLE_KEYS")


def test_the_withheld_list_has_no_stale_entries() -> None:
    """A key nothing constructs any more is a comment pretending to be a rule."""
    stale = _WITHHELD_ON_PURPOSE - _payload_keys()
    assert stale == set(), f"no payload builder produces these any more: {sorted(stale)}"


def test_every_downstream_key_really_is_added_downstream() -> None:
    """The escape hatch must not become a dumping ground.

    Each entry has to be genuinely absent from the payload modules (or it should
    just be harvested) and genuinely on the whitelist (or it is not a payload
    key at all).
    """
    renderable = _js_set("RENDERABLE_KEYS")
    harvested = _payload_keys()
    for key in sorted(_ADDED_DOWNSTREAM):
        assert key in renderable, f"{key} is exempted but not renderable"
    redundant = _ADDED_DOWNSTREAM & harvested
    assert redundant == set(), (
        f"these are harvested normally and need no exemption: {sorted(redundant)}"
    )


def test_the_capability_map_is_redacted_in_key_position() -> None:
    """`write_capability` is `{calendar_id: state}` — the id is the *key*.

    A value-only whitelist cannot see it, which is precisely how it survived the
    first cut of this fix. Pinned here because the shape is easy to reintroduce
    elsewhere and the failure is invisible from the whitelist alone.
    """
    assert "write_capability" in _js_set("KEYED_BY_SENSITIVE")
    # It stays *renderable* — its values (the states) are the diagnostic content;
    # only the keys are replaced. Dropping the whole map would cost real signal.
    assert "write_capability" in _js_set("RENDERABLE_KEYS")


def test_the_dump_is_the_only_generic_payload_serialiser_left_unredacted() -> None:
    """The criterion's "any other generic payload dump" clause, made checkable.

    `renderFamilyDetail` is the one place a whole family payload is serialised.
    If a second `traceField(..., run.summary)`-shaped call appears, it needs the
    same treatment and this test is where that gets noticed.
    """
    audit = (ROOT / "app/webapp/static/audit.js").read_text(encoding="utf-8")
    raw_dumps = re.findall(r"traceField\([^,]+,\s*(run\.summary|s)\)", audit)
    assert raw_dumps == [], f"un-redacted payload dump(s): {raw_dumps}"
    assert "redactPayload(run.summary)" in audit


def test_the_one_free_text_field_is_sanitized_at_source_not_at_the_renderer() -> None:
    """`detail` is renderable, and that is only safe because of #285's source fix.

    It is the single whitelisted key whose value is free-form text from an
    exception, and the write path used to build it from raw `str(exc)` — where
    `MarkerGuardError` prints two calendar ids and a propagated googleapiclient
    `HttpError` prints the request URI containing the URL-encoded id. Withholding
    it would have gutted the dump's most useful field; sanitizing it upstream
    keeps both properties. `tests/test_travel_blocks_write.py` drives the real
    code to prove it; this pins the whitelist's side of the bargain.
    """
    assert "detail" in _js_set("RENDERABLE_KEYS")
    write_path = (ROOT / "src/family/travel_blocks_write.py").read_text(encoding="utf-8")
    assert "safe_error_detail" in write_path
    assert "str(exc)" not in write_path
