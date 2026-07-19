"""Browser e2e for one-tap promotion from the Stage-1 tripwire (#196)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.smoke
def test_tripwire_card_promotes_candidate_in_one_tap(page: Page, base_url: str) -> None:
    chats_payload = {
        "chats": [
            {
                "id": 2,
                "source": "whatsapp",
                "source_chat_id": "e2e-g2",
                "name": "School Parents Group",
                "alias": None,
                "type": "group",
                "status": "discovered",
                "count": 1,
                "last_message_at": "2026-07-19T09:00:00+00:00",
                "last_message_text": "Sanitized reminder",
                "parent_chat_id": None,
            }
        ]
    }
    payload = {
        "window_days": 7,
        "scanned_messages": 1,
        "truncated": False,
        "hits": [
            {
                "id": 2,
                "source": "whatsapp",
                "name": "School Parents Group",
                "last_message_at": "2026-07-19T09:00:00+00:00",
                "matched_messages": 1,
                "roots": ["urgent", "deadline"],
                "buckets": ["actionable"],
            }
        ],
    }
    page.route("**/api/chats", lambda route: route.fulfill(json=chats_payload))
    page.route("**/api/chats/tripwire", lambda route: route.fulfill(json=payload))
    page.route(
        "**/api/chats/2/status",
        lambda route: route.fulfill(json={"id": 2, "status": "monitored", "baselined": True}),
    )

    page.goto(base_url)
    page.locator("#tabChats").click()

    card = page.locator("#tripwireCard")
    expect(card).to_be_visible()
    expect(card).to_contain_text("Chats worth monitoring")
    suggestion = card.locator(".tripwire-row")
    expect(suggestion).to_contain_text("School Parents Group")
    expect(suggestion).to_contain_text("urgent")
    suggestion.get_by_role("button", name="Monitor").click()
    expect(card).to_be_hidden()
