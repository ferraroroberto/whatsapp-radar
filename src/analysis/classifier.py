"""Classifiers turn a message delta into raw JSON matching the analysis contract.

Two implementations:

- :class:`StubClassifier` — deterministic, keyword-based, no network. Default for
  development and the entire test suite, so nothing depends on a running hub.
- :class:`HubClassifier` — routes through the local LLM hub via the Anthropic SDK
  (``base_url`` -> ``127.0.0.1:8000``, ``model="claude_sonnet"``). Wired but
  opt-in (``WR_CLASSIFIER=hub``).

Both return a raw JSON *string*; the review engine always validates it through
:func:`src.analysis.contract.parse_analysis`, so a malformed hub
response is handled exactly like any other contract violation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.analysis.keywords import has_actionable_signal
from src.analysis.prompts import load_prompt
from src.config import HubConfig
from src.models import StoredMessage

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


@dataclass(frozen=True)
class ClassificationOutcome:
    """A classification plus the trace metadata the audit log records.

    ``raw_output`` is the JSON string the contract parser consumes. The optional
    prompt/response fields are populated by LLM-backed classifiers so the trace
    can show the *exact* prompt sent and the *raw* model response; deterministic
    classifiers (e.g. the stub) leave them ``None`` and set ``llm_called=False``.
    """

    raw_output: str
    llm_called: bool = False
    system_prompt: str | None = None
    user_prompt: str | None = None
    raw_response: str | None = None
    # The model's stop reason (Anthropic shape: 'end_turn', 'max_tokens', …).
    # ``"max_tokens"`` means the response was truncated before completing — the
    # pipeline uses this to distinguish a budget overrun from malformed JSON.
    stop_reason: str | None = None


@runtime_checkable
class Classifier(Protocol):
    """Maps a chat's message delta to raw JSON output for the contract parser."""

    def classify(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> str:
        ...


@runtime_checkable
class TracedClassifier(Protocol):
    """A classifier that also reports trace metadata for the audit log.

    Kept separate from :class:`Classifier` so the review engine and its test
    doubles only need the plain ``classify`` method, while the scan pipeline can
    require the richer ``classify_traced``.
    """

    def classify_traced(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> ClassificationOutcome:
        ...


class StubClassifier:
    """Deterministic keyword classifier — no LLM, no network."""

    def classify_traced(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> ClassificationOutcome:
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
        return ClassificationOutcome(raw_output=json.dumps(result, ensure_ascii=False))

    def classify(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> str:
        return self.classify_traced(chat_display_name, delta, prior_context).raw_output


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
        header = [f"Chat: {chat_display_name}"]
        if prior_context:
            header.append(f"Prior context: {prior_context}")
        header.append("New messages:")

        formatted = [
            f"- [{msg.source_message_id}] {msg.sender_label or 'unknown'}: {msg.text or ''}"
            for msg in delta
        ]
        # Cap the delta so a whole-history scan can't build a single prompt that
        # blows the model's context window. Keep the most recent messages (most
        # relevant for triage) by walking newest-first until the char budget is
        # spent, then note how many older ones were dropped.
        budget = self._hub.max_prompt_chars
        kept_reversed: list[str] = []
        used = 0
        for line in reversed(formatted):
            # Always keep at least the most recent message, truncating it if a
            # single message alone exceeds the budget.
            candidate = line[:budget] if not kept_reversed and len(line) > budget else line
            if kept_reversed and used + len(candidate) + 1 > budget:
                break
            kept_reversed.append(candidate)
            used += len(candidate) + 1

        kept = list(reversed(kept_reversed))
        omitted = len(formatted) - len(kept)
        if omitted:
            kept.insert(0, f"[... {omitted} older message(s) omitted to fit the context budget]")
        return "\n".join(header + kept)

    def classify_traced(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> ClassificationOutcome:
        # Lazy import so the stub/default path needs no SDK import at module load.
        from anthropic import Anthropic

        user_prompt = self._build_user_prompt(chat_display_name, delta, prior_context)
        client = Anthropic(api_key="local-dummy", base_url=self._hub.base_url)
        response = client.messages.create(
            model=self._hub.model,
            # Output budget is configurable per model (hub.max_tokens) rather than
            # a single hard-coded value: the default model (claude_sonnet) answers
            # with JSON directly, but a reasoning model that emits a long <think>
            # trace before the JSON can overrun a small budget and truncate
            # mid-think, yielding nothing parseable. When that happens the pipeline
            # records a distinct 'llm_truncated' state via ``stop_reason`` below.
            max_tokens=self._hub.max_tokens,
            # Triage must be stable: identical messages must classify identically,
            # so pin temperature to 0 rather than leaving the model's default.
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        parts = [
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", "") == "text"
        ]
        raw_response = "".join(parts)
        # Capture the raw model text AND the extracted JSON so the trace can show
        # exactly what was sent and returned, even when extraction later fails.
        # ``stop_reason`` lets the pipeline tell a budget overrun ('max_tokens')
        # apart from genuinely malformed JSON.
        return ClassificationOutcome(
            raw_output=_extract_json_object(raw_response),
            llm_called=True,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            raw_response=raw_response,
            stop_reason=getattr(response, "stop_reason", None),
        )

    def classify(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> str:
        return self.classify_traced(chat_display_name, delta, prior_context).raw_output


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


def build_stage2_classifier(name: str, hub: HubConfig) -> TracedClassifier:
    """Construct the Stage-2 classifier for the scan pipeline.

    The pipeline owns Stage 1 (the keyword prefilter) itself, so it never wants a
    :class:`CascadeClassifier` here — it wants the engine that runs *after* the
    prefilter. ``stub`` stays deterministic/offline; both ``hub`` and ``cascade``
    map to the LLM-backed :class:`HubClassifier`.
    """
    if name == "stub":
        return StubClassifier()
    if name in ("hub", "cascade"):
        return HubClassifier(hub)
    raise ValueError(f"unknown classifier: {name!r} (expected 'stub', 'hub', or 'cascade')")
