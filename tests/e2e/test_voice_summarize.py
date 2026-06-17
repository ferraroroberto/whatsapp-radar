"""Browser e2e: on-demand Summarize control in the Chats history overlay (#86).

Route-mocks the hub-backed summarize endpoint (no hub on the e2e runner) and
asserts the control shows on a long message, fetches, and renders the summary
inline — while a short message shows no control.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


def _open_class_4a_history(page: Page, base_url: str) -> None:
    page.goto(base_url)
    page.locator("#tabChats").click()
    expect(page.locator("#paneChats")).to_be_visible()
    page.locator("#chatsFilterAll").click()
    page.locator(".chat-main", has_text="Class 4A Group").first.click()
    expect(page.locator("#historyOverlay")).to_be_visible()
    expect(page.locator("#historyBody")).to_contain_text("class trip on Friday")


@pytest.mark.smoke
def test_summarize_long_message(page: Page, base_url: str) -> None:
    # Mock the hub round-trip so the test is deterministic and offline.
    page.route(
        "**/api/messages/*/summarize",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"message_id": 1, "summary": "Send the signed form and 12 euros by Thursday."}',
        ),
    )
    _open_class_4a_history(page, base_url)

    # The long message carries a Summarize control; the short ones do not.
    actions = page.locator(".summarize-action")
    expect(actions.first).to_be_visible()
    assert actions.count() == 1

    actions.first.click()
    summary = page.locator(".msg-summary")
    expect(summary).to_be_visible()
    expect(summary).to_contain_text("Send the signed form and 12 euros by Thursday.")
