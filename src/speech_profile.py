"""Speech-profile resolution for on-demand summary read-aloud.

Picks one of four logical voice profiles — ``{en,es}_{female,male}`` — for a
message's summary. Language is detected deterministically from the message's
own *original* text (never the summary, which can be short or in a different
register), using the same :func:`src.language_detect.detect_language_code` the
Whisper language hint uses. Gender comes from an explicit per-sender mapping in the
gitignored webapp config, falling back to a single configured default — never
a name-based guess (an unofficial/uncertain signal the issue explicitly rules
out).

``src/tts_client.py`` stays a thin HTTP client with no opinion on which
profile to use; this module owns the *selection*, and ``src/config/tts.py``'s
``TtsConfig`` owns the concrete model/voice each profile key maps to.
"""

from __future__ import annotations

from src.language_detect import detect_language_code

LANGUAGES = ("en", "es")
GENDERS = ("female", "male")

DEFAULT_LANGUAGE = "en"


def detect_language(text: str) -> str:
    """Return ``'es'`` if ``text`` is confidently Spanish, else ``'en'``.

    This feature only distinguishes English and Spanish (out of scope: any
    other language), so the documented fallback — too little text to detect
    reliably, a detector error, or any detected language other than Spanish —
    is always English.
    """
    return "es" if detect_language_code(text) == "es" else DEFAULT_LANGUAGE


def normalize_sender(sender_label: str | None) -> str:
    """The lookup key a sender's voice-gender mapping is keyed on."""
    return (sender_label or "").strip().lower()


def resolve_gender(
    sender_label: str | None,
    *,
    sender_voice_genders: dict[str, str],
    default_gender: str,
) -> str:
    """An explicit mapping for the normalized sender wins; otherwise the default.

    Never infers gender from the sender's name — an unmapped or unlabeled
    sender always falls back to ``default_gender``.
    """
    key = normalize_sender(sender_label)
    if key and key in sender_voice_genders:
        return sender_voice_genders[key]
    return default_gender


def resolve_profile_key(
    original_text: str,
    sender_label: str | None,
    *,
    sender_voice_genders: dict[str, str],
    default_gender: str,
) -> str:
    """One of ``'en_female' | 'en_male' | 'es_female' | 'es_male'`` for a message."""
    language = detect_language(original_text)
    gender = resolve_gender(
        sender_label,
        sender_voice_genders=sender_voice_genders,
        default_gender=default_gender,
    )
    return f"{language}_{gender}"
