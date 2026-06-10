"""Strict validation of the LLM JSON contract."""

from __future__ import annotations

import json

import pytest

from src.analysis.contract import ContractError, parse_analysis


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
    assert result.deadline_date is None


def test_resolved_deadline_date_parses() -> None:
    result = parse_analysis(
        json.dumps(
            {
                "action_required": True,
                "deadline": "tomorrow",
                "deadline_date": "2026-06-09",
                "evidence_message_ids": ["c4a-0002"],
            }
        )
    )
    assert result.deadline == "tomorrow"
    assert result.deadline_date == "2026-06-09"


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
        json.dumps({"action_required": True, "deadline_date": 20260609}),  # wrong type
    ],
)
def test_invalid_payloads_raise(payload: str) -> None:
    with pytest.raises(ContractError):
        parse_analysis(payload)
