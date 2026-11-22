"""The one ``langdetect`` wrapper: threshold, seed and failure handling live here.

Both the Whisper language hint (:mod:`src.analysis.transcription`) and the
read-aloud voice profile (:mod:`src.speech_profile`) need "what language is this
text, or don't guess"; owning the answer in one place keeps them from drifting.
"""

from __future__ import annotations

# Below this many characters, langdetect's guess is unreliable — callers fall back
# to their own default instead of guessing from noise.
MIN_DETECT_CHARS = 20


def detect_language_code(text: str | None) -> str | None:
    """Return the detected ISO language code, or ``None`` when it can't be trusted.

    ``None`` covers too little text and a detector error alike: both mean "no
    confident answer", and every caller handles that the same way (use its own
    default).
    """
    sample = (text or "").strip()
    if len(sample) < MIN_DETECT_CHARS:
        return None
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0  # deterministic results across runs/tests
        return str(detect(sample))
    except Exception:  # noqa: BLE001 — LangDetectException or import issue
        # no-silent-except-exempt: too-short and detector-error both mean "no
        # confident answer"; there is no distinct established fact being hidden.
        return None
