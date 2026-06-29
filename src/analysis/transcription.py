"""Voice-note transcription via the local-llm-hub audio endpoint (#36).

Between sync and analysis, the scan turns each downloaded voice note into real
text so the existing pipeline — which reads only ``messages.text`` — picks it up
unchanged. The flow per note:

    pending (sidecar downloaded audio)  ->  POST to the hub's Whisper proxy
      -> success: overwrite ``text`` with the transcript, mark 'done', retain audio
      -> failure: mark 'failed' (audio kept) so the next live scan retries

On success the audio is retained for ``transcription.audio_retention_days`` so it
can be played back in the Chats overlay (#86); a sweep at the start of each phase
deletes audio past that window. Set ``audio_retention_days = 0`` to revert to #36's
delete-immediately behaviour. A retained 'done' note never trips the cursor barrier
(which only holds 'pending'/'failed' notes), so retention doesn't change gating.

A never-attempted voice note older than ``transcription.window_days`` is marked
'skipped_old' and never fetched, so a fresh pairing never transcribes a long backlog.
A note that already *failed* (a transient backend outage, not backlog) keeps retrying
on every full sync up to the longer ``transcription.failed_retry_days``, so an outage
that outlasts the transcribe window still recovers; only past that does it give up
(marked 'skipped_old', audio deleted) so sensitive audio isn't kept forever (#104).
Transcription runs
*before* analysis and advances no cursor, so a voice note is never analysed as a
placeholder and the cursor never skips real content. Each note is isolated: one
failing transcription never blocks the rest — nor analysis of the other chats.

Routes through the hub directly (``requests`` -> ``/v1/audio/transcriptions`` on
:8000), the same hub-direct pattern app-launcher uses for its LLM/TTS clients —
no extra dependency and no detour through the voice-transcriber session API.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

from src.analysis._common import Progress, _emit
from src.config import Config, TranscriptionConfig
from src.db import store
from src.models import StoredMessage

logger = logging.getLogger(__name__)

# Statuses for a voice note that has been downloaded but not yet transcribed —
# its real text is still pending, so it must not be analysed as a placeholder.
_UNTRANSCRIBED = ("pending", "failed")

# Maps ``(audio_path, language)`` to transcript text. Injectable so the scan phase
# is testable with no network; the default implementation hits the hub. ``language``
# is the resolved per-chat hint (``None`` → let the backend auto-detect).
Transcriber = Callable[[Path, str | None], str]

_WS_RUN = re.compile(r"\s+")

# Below this many characters of chat text, language detection isn't trustworthy —
# we send no hint and let the backend auto-detect rather than guess from noise.
_MIN_DETECT_CHARS = 20


class TranscriptionError(Exception):
    """Raised when the hub's audio endpoint is unreachable or returns an error."""


@dataclass
class TranscriptionOutcome:
    """What one transcription phase did, surfaced in the run summary/funnel."""

    done: int = 0
    failed: int = 0
    skipped_old: int = 0
    swept: int = 0  # retained audio files deleted past the retention window (#86)


def _flatten(text: str) -> str:
    """Collapse whitespace runs — whisper-server returns one segment per line."""
    return _WS_RUN.sub(" ", text).strip()


def hold_back_untranscribed(messages: list[StoredMessage]) -> list[StoredMessage]:
    """Trim a live delta to stop *before* the earliest not-yet-transcribed voice note.

    A voice note still awaiting transcription ('pending') or whose transcription
    errored this run but whose audio is still on disk ('failed' + ``media_path``)
    must not be analysed as its "[voice note]" placeholder — and the cursor must not
    advance past it, or the real transcript produced on a later run would never be
    analysed (#36). So the delta is cut before the earliest such note *by ingestion
    id* (the cursor key); that note and anything ingested after it wait for a run
    where it has transcribed. Messages ingested before it are analysed normally and
    the cursor advances only up to them.

    A note whose audio never downloaded (``media_path is None``) is *not* held — it
    is unrecoverable, so it surfaces as a placeholder rather than wedging the chat
    forever. Caller gates this on ``transcription.enabled`` so a disabled feature
    never blocks analysis.
    """
    barrier = min(
        (
            m.id
            for m in messages
            if m.transcription_status in _UNTRANSCRIBED and m.media_path
        ),
        default=None,
    )
    if barrier is None:
        return messages
    return [m for m in messages if m.id < barrier]


def _read_as_wav(path: Path) -> tuple[bytes, str]:
    """Return ``(wav_bytes, filename)`` for the hub, transcoding if needed.

    The hub raw-passes audio to whisper-server, which only decodes **WAV** — it
    400s on the OGG/Opus container WhatsApp voice notes arrive in (verified against
    the live hub, #36). So any non-WAV input is transcoded to 16 kHz mono PCM WAV
    with ffmpeg first. Raises :class:`TranscriptionError` if ffmpeg is needed but
    not on PATH, with a clear remediation message.
    """
    if path.suffix.lower() == ".wav":
        return path.read_bytes(), path.name
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise TranscriptionError(
            f"{path.name} is not WAV and ffmpeg is not on PATH — the hub's whisper "
            "backend only accepts WAV. Install ffmpeg to transcribe voice notes."
        )
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"],
            capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise TranscriptionError(
            f"ffmpeg could not transcode {path.name}: "
            f"{detail.decode('utf-8', 'replace')[:200] or exc}"
        ) from exc
    return proc.stdout, path.stem + ".wav"


def transcribe_file(
    path: Path,
    *,
    base_url: str,
    model: str,
    language: str = "auto",
    timeout: float = 120.0,
    session: requests.Session | None = None,
) -> str:
    """Transcribe one audio file via the hub's OpenAI-shape Whisper endpoint.

    Transcribe-only (never ``task=translate``). ``language="auto"`` sends no
    language hint, so Whisper detects each note independently (right for mixed
    ES/EN); any other value is forwarded as the ISO hint. Non-WAV audio is
    transcoded to WAV first (see :func:`_read_as_wav`). Returns the flattened
    transcript; raises :class:`TranscriptionError` on transport/HTTP failure.
    """
    url = f"{base_url.rstrip('/')}/v1/audio/transcriptions"
    data: dict[str, str] = {"model": model, "response_format": "json"}
    if language and language.lower() != "auto":
        data["language"] = language
    wav_bytes, filename = _read_as_wav(path)
    post = session.post if session is not None else requests.post
    try:
        response = post(
            url, data=data, files={"file": (filename, wav_bytes, "audio/wav")}, timeout=timeout
        )
    except requests.RequestException as exc:
        raise TranscriptionError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise TranscriptionError(f"hub returned {response.status_code}: {response.text[:500]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise TranscriptionError(f"hub returned non-JSON: {response.text[:200]}") from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    if text is None:
        raise TranscriptionError(f"hub response had no 'text': {payload!r}")
    return _flatten(str(text))


def _build_transcriber(cfg: TranscriptionConfig) -> Transcriber:
    """A hub-backed :data:`Transcriber` bound to one reusable HTTP session."""
    session = requests.Session()

    def _transcribe(path: Path, language: str | None) -> str:
        return transcribe_file(
            path,
            base_url=cfg.audio_base_url,
            model=cfg.model,
            language=language or "auto",
            timeout=cfg.timeout_seconds,
            session=session,
        )

    return _transcribe


def detect_chat_language(conn: sqlite3.Connection, chat_id: int) -> str | None:
    """Infer a chat's language (ISO 639-1) from its stored text, or ``None``.

    Chats are single-language in practice, so the chat's own typed text is a strong
    prior for the language of a voice note in it — used as the Whisper ``language``
    hint so a note transcribes in its real language regardless of any backend
    auto-detect bias (#36). Placeholder rows (``[voice note]``, ``[image]``, …) are
    excluded; below :data:`_MIN_DETECT_CHARS` of text we return ``None`` (too little
    to be sure) and let the backend auto-detect.
    """
    rows = conn.execute(
        "SELECT text FROM messages WHERE chat_id = ? AND text IS NOT NULL "
        "AND substr(text, 1, 1) != '[' ORDER BY id DESC LIMIT 40",
        (chat_id,),
    ).fetchall()
    sample = " ".join(r["text"] for r in rows if r["text"]).strip()
    if len(sample) < _MIN_DETECT_CHARS:
        return None
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0  # deterministic results across runs/tests
        return str(detect(sample))
    except Exception:  # noqa: BLE001 — LangDetectException or import issue → just skip the hint
        return None


def _resolve_language(
    conn: sqlite3.Connection,
    cfg: TranscriptionConfig,
    chat_id: int,
    cache: dict[int, str | None],
) -> str | None:
    """The Whisper language hint for a note: an explicit config pin wins; otherwise
    the chat's detected language (cached per run). ``None`` → backend auto-detect."""
    if cfg.language and cfg.language.lower() != "auto":
        return cfg.language
    if chat_id not in cache:
        cache[chat_id] = detect_chat_language(conn, chat_id)
    return cache[chat_id]


def _delete_audio(buffer_dir: Path, media_path: str | None) -> None:
    """Best-effort delete of a transcribed/skipped voice note's audio file."""
    if not media_path:
        return
    try:
        (buffer_dir / media_path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("⚠️ could not delete audio %s: %s", media_path, exc)


def run_transcription_phase(
    conn: sqlite3.Connection,
    config: Config,
    *,
    transcriber: Transcriber | None = None,
    progress: Progress | None = None,
    now: datetime | None = None,
) -> TranscriptionOutcome:
    """Transcribe pending voice notes within the window; skip older ones.

    No-op (empty outcome) when transcription is disabled. Audio paths resolve
    relative to ``config.linked_device_dir`` (the sidecar's buffer dir). Each note
    is handled independently — a failure marks that row 'failed' (retried next run)
    and never raises out of the phase, so analysis of the rest always proceeds.
    """
    outcome = TranscriptionOutcome()
    cfg = config.transcription
    if not cfg.enabled:
        return outcome

    buffer_dir = config.linked_device_dir
    window = cfg.window_days
    failed_window = cfg.failed_retry_days
    retain_days = cfg.audio_retention_days

    # 0) Retention sweep (#86): drop audio for notes transcribed more than
    # `retain_days` ago. The transcript and 'done' status stay; only the file and
    # `media_path` go, so playback expires cleanly. Skipped when retention is off
    # (then no audio is ever retained in the first place).
    if retain_days > 0:
        for row in store.expired_retained_audio(conn, retain_days=retain_days, now=now):
            _delete_audio(buffer_dir, row["media_path"])
            store.clear_media_path(conn, int(row["id"]))
            outcome.swept += 1

    # 1) Backlog guard: never-attempted notes older than the window are skipped; a
    # note that already *failed* is given a longer leash (it's a transient outage, not
    # backlog) and only skipped once it's older than `failed_window` (#104).
    for row in store.stale_voice_notes(
        conn, within_days=window, failed_within_days=failed_window, now=now
    ):
        _delete_audio(buffer_dir, row["media_path"])
        store.mark_transcription(conn, int(row["id"]), status="skipped_old")
        outcome.skipped_old += 1

    pending = store.pending_transcriptions(
        conn, within_days=window, failed_within_days=failed_window, now=now
    )
    if not pending:
        notes = []
        if outcome.skipped_old:
            notes.append(f"skipped {outcome.skipped_old} old voice note(s)")
        if outcome.swept:
            notes.append(f"swept {outcome.swept} expired audio file(s)")
        if notes:
            _emit(progress, "• transcription: " + ", ".join(notes))
        return outcome

    transcribe = transcriber if transcriber is not None else _build_transcriber(cfg)
    lang_cache: dict[int, str | None] = {}
    for row in pending:
        msg_id = int(row["id"])
        audio = buffer_dir / row["media_path"]
        if not audio.exists():
            # Audio gone (cleared out of band) — can't transcribe; mark failed so it
            # isn't retried forever against a phantom file.
            logger.warning("⚠️ voice note %s: audio file missing (%s)", msg_id, audio)
            store.mark_transcription(conn, msg_id, status="failed")
            outcome.failed += 1
            continue
        language = _resolve_language(conn, cfg, int(row["chat_id"]), lang_cache)
        try:
            transcript = transcribe(audio, language)
        except Exception as exc:  # noqa: BLE001 — isolate: one failure can't block the rest
            logger.warning("⚠️ voice note %s transcription failed: %s", msg_id, exc)
            store.mark_transcription(conn, msg_id, status="failed")
            outcome.failed += 1
            continue
        keep_media = retain_days > 0
        store.mark_transcription(
            conn, msg_id, status="done", transcript=transcript, keep_media=keep_media
        )
        if not keep_media:
            _delete_audio(buffer_dir, row["media_path"])
        outcome.done += 1

    _emit(
        progress,
        f"• transcription: {outcome.done} done, {outcome.failed} failed"
        + (f", {outcome.skipped_old} skipped (old)" if outcome.skipped_old else "")
        + (f", {outcome.swept} audio swept" if outcome.swept else ""),
    )
    return outcome
