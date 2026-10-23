"""Stage-1 multilingual keyword prefilter (ES/EN/CA). Pure text matching, no hub."""

from __future__ import annotations

from src.analysis.keywords import has_actionable_signal, matched_rules
from src.models import StoredMessage


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


def test_prefilter_matches_spanish_english_catalan() -> None:
    assert matched_rules("Hay que pagar la cuota antes del viernes")  # ES
    assert matched_rules("Please confirm the homework deadline")  # EN
    assert matched_rules("Cal portar el justificant divendres")  # CA
    assert matched_rules("Recordatorio: reunión de tutoría")  # accents stripped


def test_prefilter_ignores_noise() -> None:
    assert not matched_rules("jajaja qué bueno 😂")
    assert not matched_rules("Good morning everyone!")
    assert not matched_rules(None)


def test_has_actionable_signal_over_delta() -> None:
    assert has_actionable_signal([_msg("m1", "small talk"), _msg("m2", "firma el permiso")])
    assert not has_actionable_signal([_msg("m1", "hola"), _msg("m2", "ok 👍")])
