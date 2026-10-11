"""Browser e2e: Activity runs a dry-run scan, follows it live, and drills in.

Drives the real PWA against the autobooted webapp (stub classifier + fixture
connector, sanitized fixture DB): open Activity's Run-now sheet, switch Dry run
on, fire the full pipeline, and assert the run sheet follows the run to
completion with its funnel, per-source split, Telegram text and raw output;
then the run sits in the list with a Dry run chip and its per-chat trace opens
— the Run tab's "dry-run shows the funnel" and the Audit tab's "open a run,
see the per-chat trace" acceptance, one story since #373 merged the two tabs.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect


# Not @pytest.mark.live_safe (issue #225): clicks #execRunScan, firing the
# real backend pipeline unmocked — under WR_E2E_LIVE this would run whatever
# connector/classifier the live tray is actually configured with, not the
# stub/fixture env autoboot injects. Stays out of live mode until mocked.
@pytest.mark.smoke
def test_activity_dry_run_follows_the_run_and_drills_into_the_trace(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    page.goto(base_url)
    page.locator("#tabActivity").click()
    expect(page.locator("#paneActivity")).to_be_visible()
    # The traffic card moved to Family, Maintenance to Settings (#164, #337, #373).
    expect(page.locator("#paneActivity #execTrafficCard")).to_have_count(0)
    expect(page.locator("#settingsDialog #execMaintenanceCard")).to_have_count(1)

    # 1. Run now is one tap from the header; Dry run is a switch that reveals
    #    the replay window.
    page.locator("#activityRunNow").click()
    expect(page.locator("#runNowSheet")).to_be_visible()
    expect(page.locator("#execDryOpts")).to_be_hidden()
    page.locator("#execDryRun").click()
    expect(page.locator("#execDryRun")).to_have_attribute("aria-checked", "true")
    expect(page.locator("#execDryOpts")).to_be_visible()
    page.locator("#execRunScan").click()

    # 2. The Run-now sheet closes and the run sheet follows the run live to a
    #    terminal state (stub classifier → seconds; generous headroom for the
    #    cold subprocess).
    expect(page.locator("#runNowSheet")).to_be_hidden()
    sheet = page.locator("#runSheetCard")
    expect(sheet).to_be_visible(timeout=scaled(15_000))
    expect(page.locator("#runSheetMeta")).to_contain_text("completed", timeout=scaled(45_000))
    expect(page.locator("#runFunnel")).to_contain_text("Notify")
    expect(page.locator("#runSourceFunnel")).to_contain_text("WhatsApp")
    expect(page.locator("#runSourceFunnel")).to_contain_text("Gmail")
    # Raw output sits in a closed disclosure; Stop shows only while live.
    expect(page.locator("#runOutputCard")).not_to_have_attribute("open", "")
    page.locator("#runOutputCard summary").click()
    expect(page.locator("#runOutput")).to_contain_text("dry_run")
    expect(page.locator("#runSheetFoot")).to_be_hidden()
    page.locator("#runSheetClose").click()

    # 3. The run is in the Messages list with an exception chip for Dry run and
    #    no OK or Live stamp.
    row = page.locator("#activityList .run-row", has_text="Message scan").first
    expect(row).to_be_visible(timeout=scaled(10_000))
    chips = row.locator(".status-chip")
    expect(chips.filter(has_text="Dry run")).to_have_attribute("data-tone", "neutral")
    expect(chips.filter(has_text="actionable")).to_have_attribute("data-tone", "accent")
    expect(row).not_to_contain_text("OK")
    expect(row).not_to_contain_text("Live")

    # 4. Drill in: the sheet's funnel and at least one per-chat trace carrying the
    #    seeded monitored chat's name; expanding it reveals the per-message
    #    breakdown (#12), each message with its own Stage-1 / LLM verdict badge.
    row.locator(".action-row-main").click()
    expect(sheet).to_be_visible(timeout=scaled(10_000))
    expect(page.locator("#runFunnel")).to_contain_text("Stage 1")
    trace = page.locator("#runTraces .audit-trace").first
    expect(trace).to_be_visible(timeout=scaled(10_000))
    expect(trace).to_contain_text("Class 4A Group")
    trace.locator("summary").click()
    expect(trace).to_contain_text("WhatsApp · Stage 1")
    expect(trace).to_contain_text("Messages (")
    messages = trace.locator(".audit-msg")
    expect(messages.first).to_be_visible()
    expect(messages.first.locator(".audit-msg-badge").first).to_be_visible()


@pytest.mark.smoke
@pytest.mark.live_safe
def test_settings_sources_card_offers_reconnect_when_offline(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    """With an empty sidecar buffer the health card offers a relaunch (#29).

    Sources health lives in Settings since #373. Asserts the offline affordance
    *renders* — it never clicks Reconnect, so no Node process is ever spawned.
    """
    page.route(
        "**/api/execution/health",
        lambda route: route.fulfill(
            json={
                "name": "multi-source",
                "connected": False,
                "detail": "sanitized e2e status",
                "sources": [
                    {
                        "source": "whatsapp", "name": "fixture", "enabled": True,
                        "configured": True, "authorized": True, "connected": True,
                        "detail": "fixture", "stored_channels": 2, "stored_messages": 4,
                        "monitored_channels": 1, "latest_stored_at": None,
                        "last_attempt": None, "last_success": None,
                    },
                    {
                        "source": "gmail", "name": "gmail", "enabled": True,
                        "configured": True, "authorized": True, "connected": True,
                        "detail": "sanitized", "stored_channels": 1, "stored_messages": 1,
                        "monitored_channels": 1, "latest_stored_at": None,
                        "last_attempt": None, "last_success": None, "read_only": True,
                        "account": "f***@example.test", "token_present": True,
                        "whitelist": {"senders": [], "labels": []},
                        "whitelist_valid": True, "history_scope": "Sanitized test scope.",
                    },
                ],
            }
        ),
    )
    page.goto(base_url)
    page.locator("#tabActivity").click()
    page.locator("#paneActivity .home-settings").click()
    expect(page.locator("#settingsDialog")).to_be_visible()

    # The throwaway buffer (conftest) has no status.json → 'stopped' → a red
    # "Not connected" status word and a visible Reconnect button; the card
    # opens itself when the source is down.
    expect(page.locator("#execReconnect")).to_be_visible(timeout=scaled(10_000))
    expect(page.locator("#execReconnectBtn")).to_be_visible()
    whatsapp = page.locator(".source-status-card", has_text="WhatsApp")
    expect(whatsapp).to_be_visible()
    expect(whatsapp.locator(".source-status-state")).to_have_text("Not connected")
    expect(page.locator("#execSources use[href='#i-mail']")).to_have_count(1)
