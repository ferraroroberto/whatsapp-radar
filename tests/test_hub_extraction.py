"""Extraction of JSON from reasoning-model output (offline; no hub needed)."""

from __future__ import annotations

import json

from src.analysis.classifier import _extract_json_object
from src.analysis.contract import parse_analysis


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
