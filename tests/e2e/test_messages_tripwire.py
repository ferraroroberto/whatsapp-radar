"""Browser e2e for one-tap promotion from the Stage-1 tripwire (#196)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.smoke
@pytest.mark.live_safe
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
    # "Worth a look" sits above the list (#375), its scope note in the tooltip.
    expect(page.locator("#tripwireTitle")).to_have_text("Worth a look")
    expect(page.locator("#tripwireTitle")).to_have_attribute("title", re.compile("Stage 1 only"))
    assert card.evaluate(
        "el => el.compareDocumentPosition(document.getElementById('chatsList'))"
        " & Node.DOCUMENT_POSITION_FOLLOWING"
    ), "Worth a look must sit above the chat list"

    suggestion = card.locator(".tripwire-row")
    # The promote is the vendored icon-button: nothing painted at rest (COMP-05).
    promote_bg = suggestion.locator(".tripwire-promote").evaluate(
        "el => getComputedStyle(el).backgroundColor"
    )
    assert promote_bg == "rgba(0, 0, 0, 0)", promote_bg
    expect(suggestion).to_contain_text("School Parents Group")
    expect(suggestion).to_contain_text("urgent")
    suggestion.get_by_role("button", name="Not monitored — tap to monitor").click()
    expect(card).to_be_hidden()
