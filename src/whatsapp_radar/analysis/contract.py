"""Strict JSON contract for LLM classification output.

The classifier must return JSON with exactly the agreed fields. Parsing is
strict on purpose: if the model returns malformed or out-of-range output,
:func:`parse_analysis` raises :class:`ContractError`, and the caller must NOT
advance the chat cursor (so the same delta is retried on the next run).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

_VALID_PRIORITIES = {"low", "medium", "high"}


class ContractError(ValueError):
    """Raised when classifier output violates the analysis contract."""


@dataclass(frozen=True)
class AnalysisResult:
    action_required: bool
    priority: str | None
    summary: str | None
    suggested_next_action: str | None
    deadline: str | None
    confidence: float | None
    evidence_message_ids: list[str]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def parse_analysis(payload: str | dict[str, object]) -> AnalysisResult:
    """Validate and parse classifier output into an :class:`AnalysisResult`.

    Accepts either a JSON string or an already-decoded dict. Raises
    :class:`ContractError` on any contract violation.
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ContractError(f"output is not valid JSON: {exc}") from exc
    else:
        data = payload

    _require(isinstance(data, dict), "output must be a JSON object")
    assert isinstance(data, dict)  # for type-checkers

    _require("action_required" in data, "missing 'action_required'")
    action_required = data["action_required"]
    _require(isinstance(action_required, bool), "'action_required' must be a boolean")

    priority = data.get("priority")
    if priority is not None:
        _require(
            isinstance(priority, str) and priority in _VALID_PRIORITIES,
            f"'priority' must be one of {sorted(_VALID_PRIORITIES)}",
        )

    confidence = data.get("confidence")
    if confidence is not None:
        _require(
            isinstance(confidence, (int, float)) and not isinstance(confidence, bool),
            "'confidence' must be a number",
        )
        confidence = float(confidence)
        _require(0.0 <= confidence <= 1.0, "'confidence' must be within [0, 1]")

    evidence = data.get("evidence_message_ids", [])
    _require(isinstance(evidence, list), "'evidence_message_ids' must be a list")
    _require(
        all(isinstance(x, str) for x in evidence),
        "'evidence_message_ids' must contain only strings",
    )

    for key in ("summary", "suggested_next_action", "deadline"):
        value = data.get(key)
        _require(value is None or isinstance(value, str), f"'{key}' must be a string or null")

    return AnalysisResult(
        action_required=action_required,
        priority=priority,
        summary=data.get("summary"),
        suggested_next_action=data.get("suggested_next_action"),
        deadline=data.get("deadline"),
        confidence=confidence,
        evidence_message_ids=list(evidence),
    )
