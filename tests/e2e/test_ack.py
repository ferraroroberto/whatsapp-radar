"""Browser e2e: the Follow-ups tab lists a pending non-routine item and
acknowledges it (Step 5/5 of #206, #219).

Route-mocked (no real pipeline run needed) — mirrors test_audit.py's
list + per-item action pattern. Drives only the sanitized e2e fixture app.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect


@pytest.mark.smoke
@pytest.mark.live_safe
def test_ack_list_and_acknowledge(page: Page, base_url: str) -> None:
    items_payload = {
        "items": [
            {
                "id": 42,
                "run_id": 7,
                "chat_id": 1,
                "display_name": "School Updates",
                "child": "Sam",
                "task_category": "permission_slip",
                "summary": "Bring the signed form by Friday",
                "calendar_event_id": None,
                "status": "pending",
                "created_at": "2026-07-18T18:00:00+00:00",
                "acknowledged_at": None,
            }
        ]
    }
    acked = {"called": False}

    def handle_ack(route: Route) -> None:
        acked["called"] = True
        route.fulfill(json={"id": 42, "status": "acknowledged"})

    page.route("**/api/ack/items", lambda route: route.fulfill(json=items_payload))
    page.route("**/api/ack/42/acknowledge", handle_ack)

    page.goto(base_url)
    page.locator("#tabAck").click()
    expect(page.locator("#paneAck")).to_be_visible()

    row = page.locator("#ackItems .ack-item")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Sam")
    expect(row).to_contain_text("permission_slip")
    expect(row).to_contain_text("School Updates")
    expect(row).to_contain_text("Bring the signed form by Friday")
    expect(page.locator("#ackEmpty")).to_be_hidden()

    row.locator(".ack-btn").click()

    expect(page.locator("#ackItems .ack-item")).to_have_count(0)
    expect(page.locator("#ackEmpty")).to_be_visible()
    assert acked["called"]


@pytest.mark.smoke
@pytest.mark.live_safe
def test_ack_empty_state(page: Page, base_url: str) -> None:
    page.route("**/api/ack/items", lambda route: route.fulfill(json={"items": []}))

    page.goto(base_url)
    page.locator("#tabAck").click()

    expect(page.locator("#ackEmpty")).to_be_visible()
    expect(page.locator("#ackEmpty")).to_contain_text("No pending follow-ups")
    expect(page.locator("#ackItems .ack-item")).to_have_count(0)
