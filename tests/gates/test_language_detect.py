"""The shared language-detection helper: threshold, code, and failure handling."""

from __future__ import annotations

import pytest

from src.language_detect import MIN_DETECT_CHARS, detect_language_code

_ES = "Recuerda traer el formulario de permiso firmado mañana por la mañana."
_EN = "Please remember to bring the signed permission form tomorrow morning."


def test_returns_the_detected_code() -> None:
    assert detect_language_code(_ES) == "es"
    assert detect_language_code(_EN) == "en"


def test_too_little_text_is_none() -> None:
    assert detect_language_code(None) is None
    assert detect_language_code("   ") is None
    assert detect_language_code("x" * (MIN_DETECT_CHARS - 1)) is None


def test_detector_error_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import langdetect

    def boom(_text: str) -> str:
        raise RuntimeError("detector failed")

    monkeypatch.setattr(langdetect, "detect", boom)
    assert detect_language_code(_ES) is None
