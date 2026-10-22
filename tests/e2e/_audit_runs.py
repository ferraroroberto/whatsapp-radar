"""Shared setup for the Activity-tab audit e2e tests.

The audit sheets are all reached the same way: stub the three audit endpoints
with one canned run, load the app, and filter Activity to that run's scope. Each
test then opens the row it cares about and asserts on the sheet.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Page


def open_audit_activity(page: Page, base_url: str, run: dict[str, Any], scope: str) -> None:
    """Serve `run` as run 9 and land on Activity filtered to `scope`."""
    page.route(
        "**/api/audit/runs?*",
        lambda route: route.fulfill(json={"runs": [run], "coverage_gaps": []}),
    )
    page.route(
        "**/api/audit/filtered?*",
        lambda route: route.fulfill(
            json={"days": 30, "limit": 50, "offset": 0, "total": 0,
                  "has_more": False, "items": []}
        ),
    )
    page.route("**/api/audit/runs/9", lambda route: route.fulfill(json={"run": run, "traces": []}))
    page.goto(base_url)
    page.locator("#tabActivity").click()
    page.locator(f'#activityScope [data-scope="{scope}"]').click()
