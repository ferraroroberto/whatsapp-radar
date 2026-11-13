"""The stored calendar-scan summary reads the same through every surface."""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from app.webapp.server import create_app
from src.db import store
from src.family.scan_summary import normalize_scan_summary, scan_counts
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 50000)
LEGACY = {"conflicts": [{"a": 1}], "unknown_locations": [{"event": "x"}, {"event": "y"}]}


def test_counts_read_the_current_key() -> None:
    assert scan_counts({"conflicts": [1, 2], "missing_locations": [1]}) == (2, 1)


def test_counts_read_the_legacy_key() -> None:
    assert scan_counts(LEGACY) == (1, 2)


def test_current_key_wins_over_the_legacy_one() -> None:
    assert scan_counts({"missing_locations": [1], "unknown_locations": [1, 2, 3]}) == (0, 1)


def test_counts_of_a_missing_or_foreign_payload_are_zero() -> None:
    assert scan_counts(None) == (0, 0)
    assert scan_counts([]) == (0, 0)
    assert scan_counts({}) == (0, 0)


def test_normalize_renames_the_legacy_key_without_mutating_the_input() -> None:
    out = normalize_scan_summary(LEGACY)
    assert out["missing_locations"] == [{"event": "x"}, {"event": "y"}]
    assert "unknown_locations" not in out
    assert "unknown_locations" in LEGACY


def test_normalize_leaves_other_payloads_alone() -> None:
    current = {"missing_locations": [1]}
    assert normalize_scan_summary(current) is current
    assert normalize_scan_summary(None) is None


def test_run_endpoints_serve_the_current_key_for_a_legacy_row(tmp_path: Path) -> None:
    """The browser never sees ``unknown_locations``, so it carries no fallback for it."""
    db = tmp_path / "legacy.sqlite3"
    conn = store.connect(db)
    try:
        run_id = store.start_run(conn, mode="live", kind="calendar-scan")
        store.finish_run_summary(conn, run_id, "completed", json.dumps(LEGACY))
    finally:
        conn.close()
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    with TestClient(app, client=LOOPBACK) as client:
        listed = client.get("/api/audit/runs").json()["runs"][0]["summary"]
        detail = client.get(f"/api/audit/runs/{run_id}").json()["run"]["summary"]
        unified = client.get(f"/api/execution/runs/calendar-scan/db-{run_id}").json()["run"]
        family = client.get("/api/family").json()["last_calendar"]
        card = next(
            c for c in client.get("/api/dashboard").json()["last_activity"]
            if c["source"] == "calendar"
        )

    for summary in (listed, detail, unified["result"]):
        assert len(summary["missing_locations"]) == 2
        assert "unknown_locations" not in summary
    assert (family["conflicts"], family["missing_locations"]) == (1, 2)
    assert card["summary"] == "1 conflict · 2 missing locations"


#: The only files that may spell the legacy key: the reader itself, and the
#: browser's redaction whitelist, which names keys rather than reading them.
_MAY_NAME_LEGACY_KEY = {
    Path("src/family/scan_summary.py"),
    Path("app/webapp/static/redact.js"),
}


def test_only_the_shared_reader_knows_the_legacy_key() -> None:
    """A second reader of the old spelling is how the four copies drifted apart."""
    root = Path(__file__).resolve().parents[2]
    offenders = [
        path.relative_to(root)
        for folder, pattern in (("src", "*.py"), ("app", "*.py"), ("app", "*.js"))
        for path in (root / folder).rglob(pattern)
        if "node_modules" not in path.parts
        and "unknown_locations" in path.read_text(encoding="utf-8", errors="replace")
        and path.relative_to(root) not in _MAY_NAME_LEGACY_KEY
    ]
    assert offenders == []
