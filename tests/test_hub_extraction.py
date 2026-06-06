"""Extraction of JSON from reasoning-model output (offline; no hub needed)."""

from __future__ import annotations

import json

from src.analysis.classifier import HubClassifier, _extract_json_object
from src.analysis.contract import parse_analysis
from src.config import HubConfig
from src.models import StoredMessage


def _msg(i: int, text: str) -> StoredMessage:
    return StoredMessage(
        id=i,
        chat_id=1,
        source_message_id=f"m{i}",
        message_timestamp="2026-01-01T00:00:00+00:00",
        text=text,
        sender_label="Parent",
        message_type="text",
    )


def test_strips_think_block_and_extracts_object() -> None:
    raw = (
        "<think>\nThe message asks for payment, so action is required.\n</think>\n"
        '{"action_required": true, "priority": "high", "evidence_message_ids": ["sp-0002"]}'
    )
    extracted = _extract_json_object(raw)
    result = parse_analysis(extracted)
    assert result.action_required is True
    assert result.priority == "high"


def test_extracts_object_from_code_fence_and_prose() -> None:
    raw = 'Here is the result:\n```json\n{"action_required": false}\n```\nDone.'
    assert json.loads(_extract_json_object(raw)) == {"action_required": False}


def test_handles_braces_inside_strings() -> None:
    raw = '{"summary": "use {curly} braces", "action_required": true}'
    assert json.loads(_extract_json_object(raw))["summary"] == "use {curly} braces"


def test_no_object_returns_text_for_clean_contract_error() -> None:
    # When the model produced no JSON, return the (cleaned) text so the strict
    # contract parser raises a clear error rather than this helper masking it.
    assert _extract_json_object("<think>only reasoning</think>") == ""


# --- prompt capping --------------------------------------------------------

def _hub(max_prompt_chars: int) -> HubClassifier:
    return HubClassifier(
        HubConfig(base_url="http://x", model="m", max_prompt_chars=max_prompt_chars)
    )


def test_small_delta_is_not_capped() -> None:
    hub = _hub(24000)
    prompt = hub._build_user_prompt("Class 4A", [_msg(1, "hi"), _msg(2, "pay the fee")], None)
    assert "omitted" not in prompt
    assert "m1" in prompt and "m2" in prompt


def test_oversized_delta_keeps_most_recent_within_budget() -> None:
    # A whole-history delta of many large messages must be capped so the request
    # can't blow the model's context window; the most recent messages are kept.
    delta = [_msg(i, "x" * 500) for i in range(1, 51)]  # 50 * ~500 chars
    hub = _hub(2000)
    prompt = hub._build_user_prompt("Big History", delta, None)

    assert len(prompt) <= 2000 + 200  # budget + header/marker slack
    assert "older message(s) omitted to fit the context budget" in prompt
    # Newest kept, oldest dropped.
    assert "m50" in prompt
    assert "m1]" not in prompt


def test_single_message_larger_than_budget_is_truncated() -> None:
    hub = _hub(200)
    prompt = hub._build_user_prompt("One", [_msg(1, "y" * 5000)], None)
    assert len(prompt) <= 200 + 200  # header slack; the lone message is truncated
