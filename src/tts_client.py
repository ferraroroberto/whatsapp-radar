"""Thin local-llm-hub text-to-speech client for summary playback (#94).

The Chats overlay sends an ephemeral summary to the hub's OpenAI-shape
``POST /v1/audio/speech`` endpoint. The router streams headerless PCM16 to the
browser, which plays it progressively through Web Audio so iOS Safari does not
have to decode the hub's open-ended streaming WAV container.

This mirrors App Launcher's ``src/tts_client.py`` rather than introducing a
second subprocess or provider integration. The hub remains the single owner of
model loading, routing, and observability.
"""

from __future__ import annotations

from typing import Any

from src import _loopback_http

# The hub's current explicit expressive TTS id and documented default voice.
# Pin the registry id rather than the rotating ``audio_speech`` role so summary
# playback keeps the natural Orpheus voice App Launcher established.
DEFAULT_MODEL = "orpheus-tts"
DEFAULT_VOICE = "tara"

# Known voices per hub TTS model, so an unrecognized voice falls back to a
# valid voice *for the requested model* rather than silently substituting a
# different model's default (#157: Spanish requests use "kokoro-tts", whose
# voices aren't valid Orpheus voices and vice versa).
VOICES_BY_MODEL: dict[str, tuple[str, ...]] = {
    DEFAULT_MODEL: ("tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe"),
    "kokoro-tts": ("ef_dora", "em_alex"),
}
# Backwards-compatible alias for the default model's voice set.
VALID_VOICES = VOICES_BY_MODEL[DEFAULT_MODEL]

_HEALTH_TIMEOUT = 5.0


class TtsError(_loopback_http.LoopbackError):
    """Raised when local-llm-hub is unreachable or returns an error."""


def health(base_url: str) -> bool:
    """Return whether the hub answers its health probe with ``status=ok``."""
    body = _loopback_http.request(
        "GET",
        f"{base_url.rstrip('/')}/health",
        error=TtsError,
        service="local-llm-hub",
        timeout=_HEALTH_TIMEOUT,
        allow_empty=True,
    )
    return bool(isinstance(body, dict) and body.get("status") == "ok")


def speech_url(base_url: str) -> str:
    """Return the hub's OpenAI-shape speech endpoint."""
    return f"{base_url.rstrip('/')}/v1/audio/speech"


def build_speech_payload(
    text: str,
    voice: str | None = None,
    model: str | None = None,
    speed: float | None = None,
) -> dict[str, Any]:
    """Build a headerless streaming-PCM speech request for the hub."""
    chosen_model = model or DEFAULT_MODEL
    known_voices = VOICES_BY_MODEL.get(chosen_model, ())
    if voice in known_voices:
        chosen_voice = voice
    else:
        chosen_voice = known_voices[0] if known_voices else DEFAULT_VOICE
    payload: dict[str, Any] = {
        "model": chosen_model,
        "input": text,
        "voice": chosen_voice,
        "response_format": "pcm",
        "stream_format": "audio",
    }
    if speed is not None:
        payload["speed"] = speed
    return payload
