"""Model/voice pairs behind the four summary read-aloud profiles (#157)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VoiceProfile:
    """A hub model + voice pair behind one logical summary-speech profile."""

    model: str
    voice: str


@dataclass(frozen=True)
class TtsConfig:
    """Model/voice pairs behind the four summary read-aloud profiles (#157).

    Keyed by ``"{language}_{gender}"``; :func:`src.speech_profile.resolve_profile_key`
    picks which one applies to a given message. English keeps the existing
    expressive ``orpheus-tts`` voices App Launcher established; Spanish uses the
    hub's ``kokoro-tts`` model, whose bundled voice pack ships a stable
    Spanish-capable female/male pair (``ef_dora`` / ``em_alex``) — no second TTS
    runtime and no prerequisite local-llm-hub change needed.
    """

    en_female: VoiceProfile = field(default_factory=lambda: VoiceProfile("orpheus-tts", "tara"))
    en_male: VoiceProfile = field(default_factory=lambda: VoiceProfile("orpheus-tts", "leo"))
    es_female: VoiceProfile = field(
        default_factory=lambda: VoiceProfile("kokoro-tts", "ef_dora")
    )
    es_male: VoiceProfile = field(default_factory=lambda: VoiceProfile("kokoro-tts", "em_alex"))

    def get(self, profile_key: str) -> VoiceProfile:
        """The :class:`VoiceProfile` for a ``"{language}_{gender}"`` key."""
        profiles: dict[str, VoiceProfile] = {
            "en_female": self.en_female,
            "en_male": self.en_male,
            "es_female": self.es_female,
            "es_male": self.es_male,
        }
        return profiles[profile_key]


def _voice_profile(
    profiles_raw: dict[str, Any], key: str, default: VoiceProfile
) -> VoiceProfile:
    entry = profiles_raw.get(key) if isinstance(profiles_raw, dict) else None
    if not isinstance(entry, dict):
        return default
    return VoiceProfile(
        model=str(entry.get("model", default.model)),
        voice=str(entry.get("voice", default.voice)),
    )


def parse(profiles_raw: dict[str, Any]) -> TtsConfig:
    defaults = TtsConfig()
    return TtsConfig(
        en_female=_voice_profile(profiles_raw, "en_female", defaults.en_female),
        en_male=_voice_profile(profiles_raw, "en_male", defaults.en_male),
        es_female=_voice_profile(profiles_raw, "es_female", defaults.es_female),
        es_male=_voice_profile(profiles_raw, "es_male", defaults.es_male),
    )
