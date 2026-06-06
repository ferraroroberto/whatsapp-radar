"""Cascade classifier: multilingual keyword prefilter gates the LLM call.

No hub/network: the inner LLM classifier is a fake that records whether it ran.
"""

from __future__ import annotations

from whatsapp_radar.analysis.classifier import CascadeClassifier
from whatsapp_radar.analysis.contract import parse_analysis
from whatsapp_radar.analysis.keywords import has_actionable_signal, message_has_signal
from whatsapp_radar.models import StoredMessage


def _msg(mid: str, text: str) -> StoredMessage:
    return StoredMessage(
        id=int(mid[-1]) if mid[-1].isdigit() else 1,
        chat_id=1,
        source_message_id=mid,
        message_timestamp="2026-06-10T10:00:00+00:00",
        text=text,
        sender_label="Someone",
        message_type="text",
    )


class _FakeInner:
    def __init__(self) -> None:
        self.called = False

    def classify(self, name: str, delta: list[StoredMessage], prior: str | None) -> str:
        self.called = True
        return '{"action_required": true, "priority": "high", "evidence_message_ids": ["x"]}'


def test_prefilter_matches_spanish_english_catalan() -> None:
    assert message_has_signal("Hay que pagar la cuota antes del viernes")  # ES
    assert message_has_signal("Please confirm the homework deadline")  # EN
    assert message_has_signal("Cal portar el justificant divendres")  # CA
    assert message_has_signal("Recordatorio: reunión de tutoría")  # accents stripped


def test_prefilter_ignores_noise() -> None:
    assert not message_has_signal("jajaja qué bueno 😂")
    assert not message_has_signal("Good morning everyone!")
    assert not message_has_signal(None)


def test_cascade_short_circuits_on_noise() -> None:
    inner = _FakeInner()
    cascade = CascadeClassifier(inner)
    out = cascade.classify("Chat", [_msg("m1", "buenos días"), _msg("m2", "gracias!")], None)
    assert inner.called is False
    assert parse_analysis(out).action_required is False


def test_cascade_calls_llm_when_signal_present() -> None:
    inner = _FakeInner()
    cascade = CascadeClassifier(inner)
    delta = [_msg("m1", "hola"), _msg("m2", "hay que pagar la excursión")]
    out = cascade.classify("Chat", delta, None)
    assert inner.called is True
    assert parse_analysis(out).action_required is True


def test_has_actionable_signal_over_delta() -> None:
    assert has_actionable_signal([_msg("m1", "small talk"), _msg("m2", "firma el permiso")])
    assert not has_actionable_signal([_msg("m1", "hola"), _msg("m2", "ok 👍")])
