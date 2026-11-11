"""The PWA and the sidecar spell the backend's status words exactly as the backend does.

The words cross a process boundary (JSON to the PWA, an NDJSON buffer from the
sidecar), so each side holds a copy; this test is what keeps the copies equal.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.analysis.source_funnel import SYNC_STATUSES
from src.connector.sidecar import STATE_CONNECTING, STATE_RUNNING, SidecarStateInfo
from src.db.runs import RUN_STATUSES
from src.db.transcription_status import TRANSCRIPTION_STATUSES

REPO = Path(__file__).resolve().parents[2]
STATUSES_JS = REPO / "app" / "webapp" / "static" / "statuses.js"
VOICE_JS = REPO / "src" / "sidecar" / "voice-status.js"
INDEX_HTML = REPO / "app" / "webapp" / "static" / "index.html"

_FROZEN = re.compile(r"export const (\w+) = Object\.freeze\(\{(.*?)\}\);", re.DOTALL)
_VALUE = re.compile(r"""\w+:\s*['"]([a-z_]+)['"]""")


def _vocabulary(source: str, name: str) -> set[str]:
    """The words of ``export const <name> = Object.freeze({...})``."""
    for found, body in _FROZEN.findall(source):
        if found == name:
            return set(_VALUE.findall(body))
    raise AssertionError(f"{name} is not declared")


# state-machine-proof: run-status-ui
def test_the_pwa_run_statuses_are_the_backend_run_statuses() -> None:
    source = STATUSES_JS.read_text(encoding="utf-8")
    assert _vocabulary(source, "RUN_STATUS") == set(RUN_STATUSES)
    # A copy that drifts is told apart from one that matches.
    drifted = source.replace("abandoned", "gone")
    assert _vocabulary(drifted, "RUN_STATUS") != set(RUN_STATUSES)


def test_the_pwa_voice_and_sync_statuses_are_the_backend_ones() -> None:
    source = STATUSES_JS.read_text(encoding="utf-8")
    assert _vocabulary(source, "VOICE_STATUS") == set(TRANSCRIPTION_STATUSES)
    assert _vocabulary(source, "SYNC_STATUS") == set(SYNC_STATUSES)


# state-machine-proof: voice-status-sidecar
def test_the_sidecar_voice_statuses_are_the_backend_ones() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")
    assert _vocabulary(source, "VOICE_STATUS") == set(TRANSCRIPTION_STATUSES)
    drifted = source.replace("skipped_old", "old")
    assert _vocabulary(drifted, "VOICE_STATUS") != set(TRANSCRIPTION_STATUSES)


# state-machine-proof: load-phase
def test_every_data_state_in_the_page_is_a_declared_load_phase() -> None:
    phases = _vocabulary(STATUSES_JS.read_text(encoding="utf-8"), "LOAD_PHASE")
    used = set(re.findall(r'data-state="([a-z_]+)"', INDEX_HTML.read_text(encoding="utf-8")))
    assert used and used <= phases
    assert "bogus" not in phases


def _info(state: str) -> SidecarStateInfo:
    return SidecarStateInfo(
        state=state, detail="", paired=True, connected=False, fresh=True,
        has_qr=False, chats=0, messages=0, last_update=None,
    )


def test_the_sidecar_state_payload_says_whether_it_is_still_linking() -> None:
    assert _info(STATE_CONNECTING).to_dict()["linking"] is True
    assert _info(STATE_RUNNING).to_dict()["linking"] is False
