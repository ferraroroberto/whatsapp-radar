"""On-demand message summarization via the local-llm-hub chat endpoint (#86).

Part B of the voice-note work: the Chats overlay offers a **Summarize** control on
any long message (a long transcribed voice note or a long typed message). Tapping
it condenses the message text to its essence — and any concrete action the reader
must take — through the hub's cheap ``claude_haiku``.

This mirrors ``app-launcher/src/llm_client.py`` (same OpenAI-shape
``POST /v1/chat/completions`` on the hub at ``http://127.0.0.1:8000``, same robust
content extraction) rather than reinventing it — per CLAUDE.md, LLM calls route
through the hub with no ``claude -p``/subprocess wrapper. Only the summary prompt
differs: App Launcher summarizes a coding reply for hands-free driving; here we
summarize a WhatsApp message for "what do I actually need to do about this".

The hub binds loopback and serves plain HTTP, so the call uses ``verify=True``
(the default) and a plain ``http://`` base. It is a blocking ``requests``
round-trip the router wraps in ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from typing import Any

from src import _loopback_http

logger = logging.getLogger(__name__)

# Cheap, fast model for a short summary — the hub routes this to the local
# Claude Code subscription (see the home-stack local-llm-hub). Same id App
# Launcher uses; the hub's underscore aliases also back this repo's classifier.
DEFAULT_MODEL = "claude_haiku"

# The reader wants the point quickly, plus anything they must act on. The prompt
# must NOT bind the model to a content type — an earlier "WhatsApp message for a
# busy inbox" framing made Haiku refuse to summarize anything it judged not to be
# one (e.g. a forwarded essay), explaining its role instead. So: summarize ANY
# text, treat action items as optional, and never refuse or ask questions.
SUMMARY_SYSTEM_PROMPT = (
    "You condense the text the user sends into a short summary so they can grasp "
    "it at a glance. Output 1-3 short sentences with the essence; if the text "
    "contains any concrete action, decision, deadline, date, time, or amount, "
    "include it. Always summarize whatever text you are given, of any kind or "
    "length — never refuse, never ask a question, never comment on the type, "
    "source, or suitability of the content, never describe your own role. Plain "
    "prose only: no markdown, no lists, no preamble such as 'Here is a summary'. "
    "Write in the same language as the text."
)

# A summary is a short generation, but allow ample headroom for a cold model
# loading its weights on the first call after the hub boots.
_SUMMARIZE_TIMEOUT = 60.0


class SummarizeError(_loopback_http.LoopbackError):
    """Raised when the local-llm-hub chat endpoint is unreachable or errors."""


def chat_url(base_url: str) -> str:
    """Upstream OpenAI-shape chat endpoint for ``POST /v1/chat/completions``."""
    return f"{base_url.rstrip('/')}/v1/chat/completions"


def build_summary_payload(text: str, model: str | None = None) -> dict[str, Any]:
    """Build the chat-completions body that asks the hub to summarize ``text``."""
    return {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": False,
    }


def _extract_content(body: Any) -> str:
    """Pull the assistant message text out of an OpenAI-shape completion.

    Returns ``""`` for any unexpected shape so the caller can raise a clean
    502 rather than leaking a KeyError to the phone. Handles both a plain
    string ``content`` and the list-of-parts form some backends return.
    """
    try:
        message = body["choices"][0]["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") in (None, "text")
        ]
        return " ".join(t for t in parts if t).strip()
    return ""


def summarize(base_url: str, text: str, model: str | None = None) -> str:
    """Return a short, action-oriented summary of ``text`` from the hub.

    Raises :class:`SummarizeError` (status 503) when the hub is unreachable, the
    upstream status when it answers ``>= 400``, or 502 when it returns an
    unparseable / empty completion.
    """
    body = _loopback_http.request(
        "POST",
        chat_url(base_url),
        error=SummarizeError,
        service="local-llm-hub",
        timeout=_SUMMARIZE_TIMEOUT,
        json=build_summary_payload(text, model=model),
        allow_empty=False,
    )
    summary = _extract_content(body)
    if not summary:
        raise SummarizeError("local-llm-hub returned an empty summary")
    return summary
