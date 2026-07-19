"""Browser e2e for sender-level Gmail monitoring on the Messages tab (#166).

Exercises the source-aware vocabulary (senders, not "channels"; "Not monitored",
not "Ignored"), the promote/demote toggle on a discovered sender, and the
sender-identity chip in the history overlay. Order-independent: the session DB is
shared across the chromium/webkit projections, so the toggle is asserted to
*flip* rather than land on an absolute state.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.smoke
def test_gmail_source_switches_vocabulary_and_promotes(page: Page, base_url: str) -> None:
    page.goto(base_url)
    page.locator("#tabChats").click()
    expect(page.locator("#paneChats")).to_be_visible()

    # Switch the source filter to Gmail: the not-monitored bucket is relabelled and
    # the count speaks of senders, not WhatsApp "channels".
    page.locator("#chatsSourceGmail").click()
    page.locator("#chatsFilterAll").click()
    expect(page.locator("#chatsFilterIgnored")).to_have_text("Not monitored")
    expect(page.locator("#chatsCount")).to_contain_text("sender")

    # The discovered sender is promotable via the same watch toggle as WhatsApp.
    row = page.locator(".chat-row", has_text="Class Newsletter")
    expect(row).to_be_visible()
    toggle = row.locator(".chat-watch")
    before = toggle.get_attribute("aria-pressed")
    toggle.click()
    expected = "false" if before == "true" else "true"
    expect(toggle).to_have_attribute("aria-pressed", expected)

    # Opening the sender shows its messages with a sender chip identifying the address.
    row.locator(".chat-main").click()
    expect(page.locator("#historyOverlay")).to_be_visible()
    expect(page.locator("#historySource")).to_have_text("Gmail")
    expect(page.locator("#historySenderChip")).to_be_visible()
    expect(page.locator("#historySenderChip")).to_contain_text("newsletter@example.com")
    page.locator("#historyClose").click()
    expect(page.locator("#historyOverlay")).to_be_hidden()

    # Switching back to WhatsApp restores the "Ignored" wording — WhatsApp UX intact.
    page.locator("#chatsSourceWhatsapp").click()
    expect(page.locator("#chatsFilterIgnored")).to_have_text("Ignored")
