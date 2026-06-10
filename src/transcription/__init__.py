"""Voice-note transcription via local-llm-hub Whisper."""

from .runner import TranscriptionOutcome, transcribe_pending

__all__ = ["TranscriptionOutcome", "transcribe_pending"]
