"""Transcription pass: pending voice rows → hub → updated message text."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from src.config import Config
from src.db import store
from src.transcription.hub import TranscriptionError, transcribe_file

Progress = Callable[[str], None]


@dataclass
class TranscriptionOutcome:
    skipped_old: int = 0
    transcribed: int = 0
    failed: int = 0


def _emit(progress: Progress | None, line: str) -> None:
    if progress is not None:
        progress(line)


def _enrich_raw(raw_json: str | None, transcript: str) -> str:
    raw: dict[str, object] = {}
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                raw = parsed
        except json.JSONDecodeError:
            pass
    if "placeholder_text" not in raw:
        raw["placeholder_text"] = "[voice note]"
    raw["transcript"] = transcript
    return json.dumps(raw, ensure_ascii=False)


def transcribe_pending(
    conn: sqlite3.Connection,
    config: Config,
    *,
    progress: Progress | None = None,
) -> TranscriptionOutcome:
    """Transcribe pending voice notes within the configured window."""
    tx = config.transcription
    outcome = TranscriptionOutcome()
    if not tx.enabled:
        return outcome

    outcome.skipped_old = store.skip_old_voice_notes(conn, tx.window_days)
    pending = store.list_pending_transcriptions(conn, tx.window_days)
    if not pending and outcome.skipped_old:
        _emit(progress, f"• skipped {outcome.skipped_old} old voice note(s)")
        return outcome
    if not pending:
        return outcome

    media_root = config.linked_device_dir
    for row in pending:
        audio_path = media_root / row.media_path
        if not audio_path.is_file():
            store.apply_transcription_failed(conn, row.id)
            outcome.failed += 1
            continue
        try:
            transcript = transcribe_file(audio_path, tx)
        except TranscriptionError:
            store.apply_transcription_failed(conn, row.id)
            outcome.failed += 1
            continue
        store.apply_transcription_done(
            conn,
            row.id,
            transcript,
            _enrich_raw(row.raw_json, transcript),
        )
        try:
            audio_path.unlink()
        except OSError:
            pass
        outcome.transcribed += 1

    if outcome.transcribed or outcome.failed or outcome.skipped_old:
        parts = []
        if outcome.transcribed:
            parts.append(f"{outcome.transcribed} transcribed")
        if outcome.failed:
            parts.append(f"{outcome.failed} failed")
        if outcome.skipped_old:
            parts.append(f"{outcome.skipped_old} skipped (old)")
        _emit(progress, f"• voice notes: {', '.join(parts)}")
    return outcome
