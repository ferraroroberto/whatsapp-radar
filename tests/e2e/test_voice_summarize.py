"""Browser e2e: on-demand Summarize control in the Chats history overlay (#86).

Route-mocks the hub-backed summarize endpoint (no hub on the e2e runner) and
asserts the control shows on a long message, fetches, and renders the summary
inline — while a short message shows no control.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

_AUDIO_MOCK = """
(() => {
  window.__audioLog = { contexts: 0, resumed: 0, buffers: 0, started: 0, closed: 0 };
  class FakeBuffer {
    constructor(length, rate) { this.duration = length / Math.max(1, rate); }
    copyToChannel() {}
  }
  class FakeNode {
    constructor() { this.buffer = null; this.onended = null; }
    connect() {}
    start() { window.__audioLog.started += 1; window.__lastNode = this; }
  }
  class FakeAudioContext {
    constructor() {
      this.currentTime = 0; this.sampleRate = 24000; this.destination = {};
      window.__audioLog.contexts += 1;
    }
    resume() { window.__audioLog.resumed += 1; return Promise.resolve(); }
    createBuffer(_channels, length, rate) {
      window.__audioLog.buffers += 1;
      return new FakeBuffer(length, rate);
    }
    createBufferSource() { return new FakeNode(); }
    close() { window.__audioLog.closed += 1; return Promise.resolve(); }
  }
  window.AudioContext = FakeAudioContext;
  window.webkitAudioContext = FakeAudioContext;
})()
"""

_PCM = b"\xc2\xff\xc0\xff\xc5\xff\xca\xff"


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


@pytest.mark.smoke
def test_play_summary_aloud_streams_pcm_through_web_audio(page: Page, base_url: str) -> None:
    page.add_init_script(_AUDIO_MOCK)
    spoken: list[str] = []
    page.route(
        "**/api/messages/*/summarize",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"message_id": 1, "summary": "Send the signed form by Thursday."}',
        ),
    )
    page.route(
        "**/api/tts/health",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"available": true}'
        ),
    )

    def capture_speech(route: Any) -> None:
        spoken.append((route.request.post_data_json or {}).get("text", ""))
        route.fulfill(
            status=200,
            content_type="audio/L16",
            headers={"X-Sample-Rate": "24000"},
            body=_PCM,
        )

    page.route("**/api/tts/speak", capture_speech)
    _open_class_4a_history(page, base_url)
    page.locator(".summarize-action").click()
    speak = page.locator(".summary-speech-action")
    expect(speak).to_be_visible()
    speak.click()
    page.wait_for_function("window.__audioLog && window.__audioLog.started >= 2")

    audio = page.evaluate("window.__audioLog")
    assert spoken == ["Send the signed form by Thursday."]
    assert audio["contexts"] == 1
    assert audio["resumed"] >= 1
    assert audio["buffers"] >= 2
    assert audio["started"] >= 2
    assert speak.get_attribute("aria-pressed") == "true"
    page.evaluate("window.__lastNode.onended()")
    expect(speak).to_have_attribute("aria-pressed", "false")
