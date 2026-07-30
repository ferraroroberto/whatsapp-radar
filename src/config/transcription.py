"""Voice-note transcription via the local-llm-hub audio endpoint (#36)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.config._shared import _as_bool


@dataclass(frozen=True)
class TranscriptionConfig:
    """Voice-note transcription via the local-llm-hub audio endpoint (#36).

    Off by default so the suite stays fully offline and the feature is opt-in (like
    the hub classifier). When enabled, the scan's transcription phase POSTs each
    downloaded voice note to ``{audio_base_url}/v1/audio/transcriptions`` (the hub's
    OpenAI-shape Whisper proxy), transcribe-only.
    """

    # Master switch. When false the transcription phase is a no-op.
    enabled: bool = False
    # Only voice notes from the last N days are transcribed; older ones are marked
    # 'skipped_old' so a fresh pairing never chews through a long backlog. This gates
    # only *never-attempted* ('pending') notes — see ``failed_retry_days`` for notes
    # we already tried and that failed.
    window_days: int = 7
    # How long a note that already *failed* transcription keeps being retried (and its
    # audio kept on disk) before we give up, mark it 'skipped_old' and delete the audio.
    # A failed note means a transient outage (e.g. the whisper backend was down, #99 /
    # local-llm-hub#147), not first-pairing backlog, so it retries on every full sync
    # regardless of ``window_days`` — but bounded here so sensitive audio isn't kept
    # forever. Deliberately longer than ``window_days`` so a multi-day outage always
    # recovers; never below it in practice (#104).
    failed_retry_days: int = 30
    # The hub's audio base URL (its :8000 proxy keeps the call in the hub's
    # observability ring); ``/v1/audio/transcriptions`` is appended by the client.
    audio_base_url: str = "http://127.0.0.1:8000"
    # OpenAI-shape model id sent in the multipart form. ``"whisper-vanilla"`` is the
    # hub's glossary-free turbo path (local-llm-hub#128): it carries no initial
    # prompt and injects ``language=auto`` server-side for requests that omit one, so
    # the source language is auto-detected. The plain turbo (``"whisper-1"`` / no
    # model row) instead carries an English tech-dictation glossary and defaults each
    # languageless request to ``en``, which Englishizes non-English notes into
    # translations — never use it here. See #88.
    model: str = "whisper-vanilla"
    # Whisper language hint. ``"auto"`` (the default) sends none, so whisper-vanilla
    # auto-detects each note's language independently — right for mixed ES/EN content.
    # Pin to an ISO code (e.g. ``"es"``) only if auto-detect proves unreliable.
    language: str = "auto"
    # Per-file transcription request timeout, seconds.
    timeout_seconds: float = 120.0
    # How many days a transcribed voice note's audio is retained on disk so it can
    # be played back in the Chats overlay (#86). After this many days from the
    # note's send time a sweep at the start of each transcription phase deletes the
    # audio and clears its ``media_path``. ``0`` reverts to #36's behaviour: delete
    # the audio immediately on a successful transcription, keep nothing. Audio is
    # more sensitive than text, so this is deliberately short by default and the
    # files never leave the gitignored linked-device buffer dir.
    audio_retention_days: int = 7


def parse(raw: dict[str, Any]) -> TranscriptionConfig:
    return TranscriptionConfig(
        enabled=_as_bool(os.environ.get("WR_TRANSCRIPTION_ENABLED"), raw.get("enabled", False)),
        window_days=int(
            os.environ.get("WR_TRANSCRIPTION_WINDOW_DAYS", raw.get("window_days", 7))
        ),
        failed_retry_days=int(
            os.environ.get(
                "WR_TRANSCRIPTION_FAILED_RETRY_DAYS", raw.get("failed_retry_days", 30)
            )
        ),
        audio_base_url=os.environ.get(
            "WR_TRANSCRIPTION_AUDIO_BASE_URL",
            raw.get("audio_base_url", "http://127.0.0.1:8000"),
        ),
        model=os.environ.get("WR_TRANSCRIPTION_MODEL", raw.get("model", "whisper-vanilla")),
        language=os.environ.get("WR_TRANSCRIPTION_LANGUAGE", raw.get("language", "auto")),
        timeout_seconds=float(
            os.environ.get("WR_TRANSCRIPTION_TIMEOUT", raw.get("timeout_seconds", 120.0))
        ),
        audio_retention_days=int(
            os.environ.get(
                "WR_TRANSCRIPTION_RETAIN_DAYS", raw.get("audio_retention_days", 7)
            )
        ),
    )
