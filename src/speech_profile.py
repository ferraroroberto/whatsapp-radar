"""Speech-profile resolution for on-demand summary read-aloud (#157).

Picks one of four logical voice profiles — ``{en,es}_{female,male}`` — for a
message's summary. Language is detected deterministically from the message's
own *original* text (never the summary, which can be short or in a different
register), using the same ``langdetect`` pattern
:func:`src.analysis.transcription.detect_chat_language` uses for the Whisper
language hint. Gender comes from an explicit per-sender mapping in the
gitignored webapp config, falling back to a single configured default — never
a name-based guess (an unofficial/uncertain signal the issue explicitly rules
out).

``src/tts_client.py`` stays a thin HTTP client with no opinion on which
profile to use; this module owns the *selection*, and ``src/config.py``'s
``TtsConfig`` owns the concrete model/voice each profile key maps to.
"""

from __future__ import annotations

# Below this many characters, langdetect's guess is unreliable — mirrors
# src/analysis/transcription.py's _MIN_DETECT_CHARS threshold.
_MIN_DETECT_CHARS = 20

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
    sample = (text or "").strip()
    if len(sample) < _MIN_DETECT_CHARS:
        return DEFAULT_LANGUAGE
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0  # deterministic results across runs/tests
        return "es" if detect(sample) == "es" else DEFAULT_LANGUAGE
    except Exception:  # noqa: BLE001 — LangDetectException or import issue
        return DEFAULT_LANGUAGE


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
