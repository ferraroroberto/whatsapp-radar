"""Strict validation of the LLM JSON contract."""

from __future__ import annotations

import json

import pytest

from whatsapp_radar.analysis.contract import ContractError, parse_analysis


def test_valid_payload_parses() -> None:
    result = parse_analysis(
        json.dumps(
            {
                "action_required": True,
                "priority": "high",
                "summary": "Pay the fee",
                "suggested_next_action": "Pay today",
                "deadline": "this evening",
                "confidence": 0.92,
                "evidence_message_ids": ["sp-0002"],
            }
        )
    )
    assert result.action_required is True
    assert result.priority == "high"
    assert result.evidence_message_ids == ["sp-0002"]


def test_minimal_payload_parses() -> None:
    result = parse_analysis(json.dumps({"action_required": False}))
    assert result.action_required is False
    assert result.evidence_message_ids == []


@pytest.mark.parametrize(
    "payload",
    [
        "{ not json",
        json.dumps(["not", "an", "object"]),
        json.dumps({"priority": "high"}),  # missing action_required
        json.dumps({"action_required": "yes"}),  # wrong type
        json.dumps({"action_required": True, "priority": "urgent"}),  # bad enum
        json.dumps({"action_required": True, "confidence": 1.5}),  # out of range
        json.dumps({"action_required": True, "evidence_message_ids": "sp-0002"}),  # not a list
        json.dumps({"action_required": True, "evidence_message_ids": [1, 2]}),  # non-string ids
        json.dumps({"action_required": True, "summary": 5}),  # wrong type
    ],
)
def test_invalid_payloads_raise(payload: str) -> None:
    with pytest.raises(ContractError):
        parse_analysis(payload)
