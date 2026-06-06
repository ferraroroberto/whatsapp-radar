"""Classifiers turn a message delta into raw JSON matching the analysis contract.

Two implementations:

- :class:`StubClassifier` — deterministic, keyword-based, no network. Default for
  development and the entire test suite, so nothing depends on a running hub.
- :class:`HubClassifier` — routes through the local LLM hub via the Anthropic SDK
  (``base_url`` -> ``127.0.0.1:8000``, ``model="agentic_light"``). Wired but
  opt-in (``WR_CLASSIFIER=hub``).

Both return a raw JSON *string*; the review engine always validates it through
:func:`whatsapp_radar.analysis.contract.parse_analysis`, so a malformed hub
response is handled exactly like any other contract violation.
"""

from __future__ import annotations

import json
import re
from typing import Protocol, runtime_checkable

from ..config import HubConfig
from ..models import StoredMessage
from .keywords import has_actionable_signal
from .prompts import load_prompt

# Returned by the cascade when the cheap prefilter finds no actionable signal, so
# no LLM call is made. Mirrors the contract the parser expects.
_NO_SIGNAL_RESULT = json.dumps(
    {
        "action_required": False,
        "priority": None,
        "summary": None,
        "suggested_next_action": None,
        "deadline": None,
        "confidence": 0.5,
        "evidence_message_ids": [],
    }
)

# Keywords that deterministically mark a message as actionable for the stub.
_ACTION_KEYWORDS = (
    "please",
    "bring",
    "pay",
    "deadline",
    "due",
    "sign",
    "signed",
    "permission",
    "form",
    "reminder",
    "rsvp",
    "register",
    "confirm",
)
_HIGH_PRIORITY_KEYWORDS = ("urgent", "today", "asap", "immediately", "now")

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _extract_json_object(text: str) -> str:
    """Best-effort extraction of the JSON object from a model's raw text.

    Reasoning models wrap their answer in ``<think>...</think>`` and may add code
    fences or prose. This strips the think block and returns the first balanced
    ``{...}`` object. Validation stays strict downstream: if nothing parseable is
    found, the original text is returned so the contract parser raises cleanly.
    """
    cleaned = _THINK_BLOCK.sub("", text).strip()
    start = cleaned.find("{")
    if start == -1:
        return cleaned
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start : i + 1]
    return cleaned


@runtime_checkable
class Classifier(Protocol):
    """Maps a chat's message delta to raw JSON output for the contract parser."""

    def classify(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> str:
        ...


class StubClassifier:
    """Deterministic keyword classifier — no LLM, no network."""

    def classify(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> str:
        evidence: list[str] = []
        high = False
        first_hit: str | None = None
        for msg in delta:
            text = (msg.text or "").lower()
            if any(kw in text for kw in _ACTION_KEYWORDS):
                evidence.append(msg.source_message_id)
                if first_hit is None:
                    first_hit = msg.text
                if any(kw in text for kw in _HIGH_PRIORITY_KEYWORDS):
                    high = True

        action_required = bool(evidence)
        result = {
            "action_required": action_required,
            "priority": ("high" if high else "medium") if action_required else None,
            "summary": (first_hit if action_required else None),
            "suggested_next_action": (
                "Review and respond in WhatsApp" if action_required else None
            ),
            "deadline": None,
            "confidence": 0.9 if action_required else 0.8,
            "evidence_message_ids": evidence,
        }
        return json.dumps(result, ensure_ascii=False)


# The system prompt is kept in an inspectable Markdown file
# (analysis/prompts/classification_system.md) so it can be reviewed and tuned
# without code changes. It is loaded verbatim at import time.
_SYSTEM_PROMPT = load_prompt("classification_system")


class HubClassifier:
    """Routes classification through the local LLM hub (opt-in)."""

    def __init__(self, hub: HubConfig) -> None:
        self._hub = hub

    def _build_user_prompt(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> str:
        lines = [f"Chat: {chat_display_name}"]
        if prior_context:
            lines.append(f"Prior context: {prior_context}")
        lines.append("New messages:")
        for msg in delta:
            sender = msg.sender_label or "unknown"
            lines.append(f"- [{msg.source_message_id}] {sender}: {msg.text or ''}")
        return "\n".join(lines)

    def classify(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> str:
        # Lazy import so the stub/default path needs no SDK import at module load.
        from anthropic import Anthropic

        client = Anthropic(api_key="local-dummy", base_url=self._hub.base_url)
        response = client.messages.create(
            model=self._hub.model,
            # agentic_light is a reasoning model that emits a long <think> trace
            # before the JSON; the budget must cover the trace AND the answer, or
            # the response is truncated mid-think and yields no parseable JSON.
            max_tokens=8192,
            # Triage must be stable: identical messages must classify identically,
            # so pin temperature to 0 rather than leaving the model's default.
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": self._build_user_prompt(chat_display_name, delta, prior_context),
                }
            ],
        )
        parts = [
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", "") == "text"
        ]
        return _extract_json_object("".join(parts))


class CascadeClassifier:
    """Two-stage classifier: a cheap keyword prefilter gates an LLM call.

    The prefilter (multilingual, ES/EN/CA) drops "utter noise" deltas without an
    LLM round-trip: if no message carries an actionable root, it returns a
    not-actionable result immediately. Otherwise it hands the whole (small) delta
    to ``inner`` — the surrounding messages give the LLM the context a single
    keyword-matching line would lack.
    """

    def __init__(self, inner: Classifier) -> None:
        self._inner = inner

    def classify(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> str:
        if not has_actionable_signal(delta):
            return _NO_SIGNAL_RESULT
        return self._inner.classify(chat_display_name, delta, prior_context)


def build_classifier(name: str, hub: HubConfig) -> Classifier:
    """Construct a classifier by config name ('stub' | 'hub' | 'cascade')."""
    if name == "stub":
        return StubClassifier()
    if name == "hub":
        return HubClassifier(hub)
    if name == "cascade":
        return CascadeClassifier(HubClassifier(hub))
    raise ValueError(f"unknown classifier: {name!r} (expected 'stub', 'hub', or 'cascade')")
