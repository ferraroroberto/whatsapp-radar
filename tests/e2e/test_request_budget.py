"""Runtime request budget: what the main page really sends in a visible and a hidden minute."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e.budget._request_budget import (
    assert_request_budget,
    measure_request_budget,
    visible_budget,
)
from tests.e2e.conftest import ROOT

# The usage log sends its queued events once when the tab hides (a beacon so they survive the
# tab going away). That is one flush of what already happened, not a poll, and it stops there.
HIDE_FLUSH_PATH = "/api/usage/events"


@pytest.mark.smoke
def test_main_page_keeps_its_request_budget(page: Page, base_url: str) -> None:
    """Visible within `[request_budget]`, nothing while hidden, one catch-up read per path."""
    reading = measure_request_budget(page, f"{base_url}/")
    assert reading.hidden[HIDE_FLUSH_PATH] <= 1, reading.summary()
    del reading.hidden[HIDE_FLUSH_PATH]
    assert_request_budget(reading, visible_budget(ROOT))
