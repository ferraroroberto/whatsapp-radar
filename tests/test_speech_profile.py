"""Speech-profile resolution for on-demand summary read-aloud (#157)."""

from __future__ import annotations

from src import speech_profile
from src.config import TtsConfig

_EN = "Please remember to bring the signed permission form tomorrow morning."
_ES = "Recuerda traer el formulario de permiso firmado mañana por la mañana."


def test_detects_spanish_from_confident_text() -> None:
    assert speech_profile.detect_language(_ES) == "es"


def test_detects_english_from_confident_text() -> None:
    assert speech_profile.detect_language(_EN) == "en"


def test_short_text_falls_back_to_english() -> None:
    assert speech_profile.detect_language("Ok") == "en"
    assert speech_profile.detect_language("") == "en"


def test_non_spanish_non_english_falls_back_to_english() -> None:
    # French, well past the detection threshold — out of scope, so it must
    # default to English rather than surfacing a third language.
    french = "Bonjour, n'oubliez pas d'apporter le formulaire signé demain matin."
    assert speech_profile.detect_language(french) == "en"


def test_normalize_sender_trims_and_lowercases() -> None:
    assert speech_profile.normalize_sender("  Teacher  ") == "teacher"
    assert speech_profile.normalize_sender(None) == ""


def test_resolve_gender_explicit_mapping_wins() -> None:
    gender = speech_profile.resolve_gender(
        "Maria", sender_voice_genders={"maria": "male"}, default_gender="female"
    )
    assert gender == "male"


def test_resolve_gender_unmapped_sender_uses_default() -> None:
    gender = speech_profile.resolve_gender(
        "Unknown Number", sender_voice_genders={"teacher": "male"}, default_gender="female"
    )
    assert gender == "female"


def test_resolve_gender_none_sender_uses_default() -> None:
    assert (
        speech_profile.resolve_gender(None, sender_voice_genders={}, default_gender="male")
        == "male"
    )


def test_resolve_gender_normalizes_before_lookup() -> None:
    gender = speech_profile.resolve_gender(
        "  TEACHER  ", sender_voice_genders={"teacher": "male"}, default_gender="female"
    )
    assert gender == "male"


def test_resolve_profile_key_combines_language_and_gender() -> None:
    key = speech_profile.resolve_profile_key(
        _ES, "Maria", sender_voice_genders={"maria": "male"}, default_gender="female"
    )
    assert key == "es_male"

    key = speech_profile.resolve_profile_key(
        _EN, "Teacher", sender_voice_genders={}, default_gender="female"
    )
    assert key == "en_female"


def test_configured_female_and_male_sender_select_matching_voice_in_both_languages() -> None:
    """A configured female sender and a configured male sender must each resolve
    to the matching gendered voice, in both English and Spanish (#157)."""
    genders = {"mom": "female", "dad": "male"}
    tts = TtsConfig()

    key = speech_profile.resolve_profile_key(
        _EN, "Mom", sender_voice_genders=genders, default_gender="male"
    )
    assert key == "en_female"
    assert tts.get(key) == tts.en_female

    key = speech_profile.resolve_profile_key(
        _ES, "Mom", sender_voice_genders=genders, default_gender="male"
    )
    assert key == "es_female"
    assert tts.get(key) == tts.es_female

    key = speech_profile.resolve_profile_key(
        _EN, "Dad", sender_voice_genders=genders, default_gender="female"
    )
    assert key == "en_male"
    assert tts.get(key) == tts.en_male

    key = speech_profile.resolve_profile_key(
        _ES, "Dad", sender_voice_genders=genders, default_gender="female"
    )
    assert key == "es_male"
    assert tts.get(key) == tts.es_male

    # Unmapped sender: no external lookup, just the configured default.
    key = speech_profile.resolve_profile_key(
        _EN, "Unknown Number", sender_voice_genders=genders, default_gender="male"
    )
    assert key == "en_male"
