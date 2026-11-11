"""The voice-note transcription status vocabulary, declared once.

``messages.transcription_status`` says where a downloaded voice note stands. The
sidecar writes the first word (``pending`` after a download, ``failed`` when the
download fails), the transcription phase moves it on, and the PWA words each state
for the reader. Everything that branches on the word asks this module.
"""

from __future__ import annotations

#: Audio downloaded, no transcript attempted yet.
TRANSCRIPTION_PENDING = "pending"
#: Transcribed: the transcript is the message text.
TRANSCRIPTION_DONE = "done"
#: An attempt errored; text and audio are kept so the next live scan retries.
TRANSCRIPTION_FAILED = "failed"
#: Too old to transcribe; the audio is dropped and the placeholder stays.
TRANSCRIPTION_SKIPPED_OLD = "skipped_old"

TRANSCRIPTION_STATUSES: frozenset[str] = frozenset(
    {
        TRANSCRIPTION_PENDING,
        TRANSCRIPTION_DONE,
        TRANSCRIPTION_FAILED,
        TRANSCRIPTION_SKIPPED_OLD,
    }
)


def awaiting_transcript(status: str | None) -> bool:
    """True while the real text is still to come, so the note is not analysed as a placeholder."""
    return status in (TRANSCRIPTION_PENDING, TRANSCRIPTION_FAILED)


def require_transcription_status(status: str, transcript: str | None) -> str:
    """Return ``status`` when a write may record it, else raise ``ValueError``.

    ``done`` carries the transcript: a ``done`` row without one would read as
    transcribed while holding the placeholder.
    """
    if status not in TRANSCRIPTION_STATUSES:
        raise ValueError(f"{status!r} is not a transcription status")
    if status == TRANSCRIPTION_DONE and transcript is None:
        raise ValueError("a 'done' transcription needs its transcript")
    return status
