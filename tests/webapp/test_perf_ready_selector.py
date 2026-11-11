"""The phone-load probe's declared `ready_selector` stays wired to the Home glance card."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"


def _selector() -> str:
    cfg = tomllib.loads((ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    return cfg["perf"]["review"]["ready_selector"]


def test_ready_selector_names_the_data_state_of_the_glance_card() -> None:
    selector = _selector()
    assert selector == '#dashGlance[data-state="ready"] .glance-row'
    # Every id/class/state the selector keys on exists in the shipped assets.
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert re.search(r'id="dashGlance"[^>]*data-state="loading"', index)
    dashboard = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    assert (
        "els.dashGlance.dataset.state = state.dashboard ? LOAD_PHASE.READY : LOAD_PHASE.LOADING"
        in dashboard
    )
    statuses = (STATIC / "statuses.js").read_text(encoding="utf-8")
    assert "READY: 'ready'" in statuses and "LOADING: 'loading'" in statuses
    assert "liClass: 'glance-row'" in dashboard


def test_glance_card_is_ready_only_after_the_dashboard_landed() -> None:
    # The state is derived from state.dashboard, not from "rows rendered": the rows
    # paint with a placeholder from the health fetch before /api/dashboard answers.
    dashboard = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    assert "fetchHealth().then(renderGlance)" in dashboard
    assert "state.dashboard = data;\n    renderGlance();" in dashboard.replace("\r\n", "\n")
