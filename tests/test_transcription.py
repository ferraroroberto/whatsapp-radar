"""Voice-note transcription phase (#36) — all offline, no network, no real audio.

A fake transcriber stands in for the hub's Whisper endpoint, and a few bytes in a
temp file stand in for a voice note's audio. Covers the contract that matters:

- a voice note's transcript overwrites ``text`` in place (placeholder preserved),
  the audio file is deleted, and the existing pipeline then flags it actionable;
- the first-run window caps transcription (older notes are skipped, not fetched);
- a transcription failure is isolated, retries next run, and the cursor never
  advances past the untranscribed note (so its real transcript is never skipped).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.analysis import transcription as tr
from src.analysis.classifier import StubClassifier
from src.analysis.pipeline import scan
from src.analysis.review import review_monitored_chats
from src.analysis.transcription import TranscriptionError, run_transcription_phase, transcribe_file
from src.config import Config, HubConfig, TelegramConfig, TranscriptionConfig
from src.db import store
from src.models import ChatRecord, MessageRecord


def _config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    window_days: int = 7,
    failed_retry_days: int = 30,
    language: str = "auto",
    audio_retention_days: int = 7,
) -> Config:
    return Config(
        db_path=tmp_path / "unused.sqlite3",
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="m"),
        notifier="none",
        telegram=TelegramConfig(bot_token="", chat_id=""),
        linked_device_dir=tmp_path / "ld",
        transcription=TranscriptionConfig(
            enabled=enabled,
            window_days=window_days,
            failed_retry_days=failed_retry_days,
            language=language,
            audio_retention_days=audio_retention_days,
        ),
    )


def _voice_note(
    conn: sqlite3.Connection,
    chat_id: int,
    *,
    msg_id: str,
    buffer_dir: Path,
    when: datetime,
    audio: bool = True,
    text: str = "[voice note]",
    status: str = "pending",
) -> Path:
    """Insert a voice-note row (default 'pending') and (optionally) a stub audio file."""
    rel = f"media/{msg_id}.ogg"
    audio_path = buffer_dir / rel
    if audio:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"OggS-fake-audio")
    store.insert_messages(
        conn,
        chat_id,
        [
            MessageRecord(
                source_message_id=msg_id,
                message_timestamp=when.isoformat(),
                text=text,
                sender_label="Parent",
                message_type="voice",
                transcription_status=status,
                media_path=rel if audio else None,
            )
        ],
    )
    return audio_path


def _monitor_with_voice(
    conn: sqlite3.Connection, buffer_dir: Path, **kw: object
) -> tuple[int, str, Path]:
    chat_id = store.upsert_chat(conn, ChatRecord(source_chat_id="c-voice", display_name="Class 4A"))
    store.set_chat_status(conn, chat_id, "monitored")  # no cursor yet → the note is the delta
    msg_id = "v1"
    audio = _voice_note(
        conn, chat_id, msg_id=msg_id, buffer_dir=buffer_dir, when=datetime.now(UTC), **kw
    )
    return chat_id, msg_id, audio


def test_transcript_overwrites_text_retains_audio_and_preserves_placeholder(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    cfg = _config(tmp_path)  # retention on by default (7 days)
    chat_id, msg_id, audio = _monitor_with_voice(conn, cfg.linked_device_dir)

    outcome = run_transcription_phase(
        conn, cfg, transcriber=lambda _p, _lang: "Please bring the signed form tomorrow"
    )

    assert (outcome.done, outcome.failed, outcome.skipped_old) == (1, 0, 0)
    row = conn.execute(
        "SELECT text, message_type, transcription_status, media_path, raw_json "
        "FROM messages WHERE chat_id = ? AND source_message_id = ?",
        (chat_id, msg_id),
    ).fetchone()
    # Transcript landed in `text`; type stays 'voice' as a UI marker.
    assert row["text"] == "Please bring the signed form tomorrow"
    assert row["message_type"] == "voice"
    assert row["transcription_status"] == "done"
    # Retention on (#86): media_path keeps pointing at the still-present audio so it
    # can be played back; the file survives a successful transcription.
    assert row["media_path"] is not None
    assert audio.exists()
    # Original placeholder preserved out-of-band in raw_json.
    assert json.loads(row["raw_json"])["placeholder_text"] == "[voice note]"


def test_retention_zero_deletes_audio_immediately_on_success(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # audio_retention_days=0 reverts to #36's delete-immediately behaviour.
    cfg = _config(tmp_path, audio_retention_days=0)
    chat_id, msg_id, audio = _monitor_with_voice(conn, cfg.linked_device_dir)

    run_transcription_phase(conn, cfg, transcriber=lambda _p, _lang: "done")

    row = conn.execute(
        "SELECT transcription_status, media_path FROM messages "
        "WHERE chat_id = ? AND source_message_id = ?",
        (chat_id, msg_id),
    ).fetchone()
    assert row["transcription_status"] == "done"
    assert row["media_path"] is None
    assert not audio.exists()


def test_retention_sweep_deletes_expired_audio_but_keeps_in_window(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # Two already-transcribed notes with retained audio: one within the window, one
    # past it. The sweep at the phase start drops only the expired one's audio and
    # clears its media_path; both keep their transcript and 'done' status.
    cfg = _config(tmp_path, audio_retention_days=7)
    buffer_dir = cfg.linked_device_dir
    chat_id = store.upsert_chat(conn, ChatRecord(source_chat_id="c-ret", display_name="Class 4A"))
    store.set_chat_status(conn, chat_id, "monitored")

    fresh = _voice_note(
        conn, chat_id, msg_id="fresh", buffer_dir=buffer_dir,
        when=datetime.now(UTC) - timedelta(days=1), text="fresh transcript",
    )
    expired = _voice_note(
        conn, chat_id, msg_id="expired", buffer_dir=buffer_dir,
        when=datetime.now(UTC) - timedelta(days=30), text="expired transcript",
    )
    # Promote both to retained 'done' rows (transcript in text, media_path kept).
    for sid in ("fresh", "expired"):
        mid = conn.execute(
            "SELECT id FROM messages WHERE source_message_id = ?", (sid,)
        ).fetchone()["id"]
        store.mark_transcription(
            conn, int(mid), status="done", transcript=f"{sid} transcript", keep_media=True
        )

    outcome = run_transcription_phase(conn, cfg, transcriber=lambda _p, _lang: "unused")

    assert outcome.swept == 1
    rows = {
        r["source_message_id"]: r
        for r in conn.execute(
            "SELECT source_message_id, transcription_status, media_path FROM messages"
        ).fetchall()
    }
    assert rows["fresh"]["media_path"] is not None and fresh.exists()
    assert rows["expired"]["media_path"] is None and not expired.exists()
    # The expired note keeps its transcript — only playback expires.
    assert rows["expired"]["transcription_status"] == "done"


def test_retained_done_note_does_not_trip_cursor_barrier(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # A retained 'done' note has a media_path but must NOT be held back: the barrier
    # only holds 'pending'/'failed' notes, so retention can't regress #36's gating.
    from src.analysis.transcription import hold_back_untranscribed

    cfg = _config(tmp_path, audio_retention_days=7)
    chat_id, msg_id, _ = _monitor_with_voice(conn, cfg.linked_device_dir)
    run_transcription_phase(conn, cfg, transcriber=lambda _p, _lang: "Bring the form")

    messages = store.messages_since_cursor(conn, chat_id)
    assert messages  # the transcribed note is present
    assert all(m.transcription_status == "done" and m.media_path for m in messages)
    # Nothing is trimmed — a retained done note flows straight into analysis.
    assert hold_back_untranscribed(messages) == messages


def test_transcribed_voice_note_flows_through_pipeline_as_actionable(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # The end-to-end acceptance: an actionable voice note, once transcribed, is
    # flagged by the *unchanged* Stage-1/Stage-2 pipeline (the stub keys on
    # "please"/"form"/"sign"). Transcription runs inside scan before analysis.
    cfg = _config(tmp_path)
    chat_id, _, _ = _monitor_with_voice(conn, cfg.linked_device_dir)

    outcome = _scan_with_transcriber(
        conn, cfg, lambda _p, _lang: "Please sign the permission form"
    )

    assert outcome.transcriptions == 1
    assert outcome.actionable == 1
    trace = conn.execute(
        "SELECT final_action, messages_json FROM analysis_trace "
        "WHERE run_id = ? AND chat_id = ?",
        (outcome.run_id, chat_id),
    ).fetchone()
    assert trace["final_action"] == "actionable"
    # The audit per-message record carries the voice type so the UI can mark it.
    msgs = json.loads(trace["messages_json"])
    assert any(m["type"] == "voice" and "permission form" in (m["text"] or "") for m in msgs)


def test_old_voice_notes_are_skipped_not_transcribed(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    cfg = _config(tmp_path, window_days=7)
    chat_id = store.upsert_chat(conn, ChatRecord(source_chat_id="c-old", display_name="Old"))
    store.set_chat_status(conn, chat_id, "monitored")
    old_audio = _voice_note(
        conn, chat_id, msg_id="old", buffer_dir=cfg.linked_device_dir,
        when=datetime.now(UTC) - timedelta(days=30),
    )

    calls: list[Path] = []

    def _transcriber(path: Path, _language: str | None) -> str:
        calls.append(path)
        return "should never run"

    outcome = run_transcription_phase(conn, cfg, transcriber=_transcriber)

    assert calls == []  # the old note was never sent to the transcriber
    assert (outcome.done, outcome.skipped_old) == (0, 1)
    row = conn.execute(
        "SELECT transcription_status, media_path FROM messages WHERE source_message_id = 'old'"
    ).fetchone()
    assert row["transcription_status"] == "skipped_old"
    assert row["media_path"] is None
    assert not old_audio.exists()  # backlog audio cleaned up, not transcribed


def test_disabled_is_a_noop(conn: sqlite3.Connection, tmp_path: Path) -> None:
    cfg = _config(tmp_path, enabled=False)
    _monitor_with_voice(conn, cfg.linked_device_dir)

    def _boom(_p: Path, _lang: str | None) -> str:
        raise AssertionError("disabled transcription must not call the hub")

    outcome = run_transcription_phase(conn, cfg, transcriber=_boom)
    assert (outcome.done, outcome.failed, outcome.skipped_old) == (0, 0, 0)
    row = conn.execute(
        "SELECT transcription_status FROM messages WHERE source_message_id = 'v1'"
    ).fetchone()
    assert row["transcription_status"] == "pending"  # untouched


def test_failure_isolated_keeps_audio_and_holds_cursor(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # A transcription failure must: mark the note 'failed' (retried next run), keep
    # its audio, and — crucially — never let analysis advance the cursor past it.
    cfg = _config(tmp_path)
    chat_id, msg_id, audio = _monitor_with_voice(conn, cfg.linked_device_dir)

    def _failing(_p: Path, _lang: str | None) -> str:
        raise RuntimeError("hub down")

    out1 = _scan_with_transcriber(conn, cfg, _failing)
    assert out1.transcriptions == 0  # nothing transcribed
    row = conn.execute(
        "SELECT transcription_status, media_path FROM messages "
        "WHERE chat_id = ? AND source_message_id = ?",
        (chat_id, msg_id),
    ).fetchone()
    assert row["transcription_status"] == "failed"
    assert row["media_path"] is not None and audio.exists()  # audio kept for retry
    # Cursor was NOT advanced past the untranscribed note.
    state = conn.execute(
        "SELECT last_processed_message_id FROM chat_review_state WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    baseline_id = _baseline_id(conn, chat_id)
    assert state is None or state["last_processed_message_id"] == baseline_id

    # Next run the hub is back: the note transcribes and is analysed (never skipped).
    out2 = _scan_with_transcriber(conn, cfg, lambda _p, _lang: "Please bring the form")
    assert out2.transcriptions == 1
    assert out2.actionable == 1


# --- whole-backend outage short-circuits the batch (#99) -------------------


def test_backend_down_short_circuits_batch_and_leaves_notes_pending(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # Three pending notes, all in different chats. A backend-down error on the
    # first one must stop the batch immediately — the other two are never even
    # sent to the transcriber, and none of the three flip to 'failed'.
    cfg = _config(tmp_path)
    buffer_dir = cfg.linked_device_dir
    notes = []
    for i in range(3):
        chat_id = store.upsert_chat(
            conn, ChatRecord(source_chat_id=f"c-outage-{i}", display_name="Class 4A")
        )
        store.set_chat_status(conn, chat_id, "monitored")
        audio = _voice_note(
            conn, chat_id, msg_id=f"n{i}", buffer_dir=buffer_dir,
            when=datetime.now(UTC) - timedelta(seconds=3 - i),
        )
        notes.append((chat_id, f"n{i}", audio))

    calls: list[Path] = []

    def _backend_down(path: Path, _language: str | None) -> str:
        calls.append(path)
        raise tr.TranscriptionBackendDown("could not reach http://hub: connection refused")

    outcome = run_transcription_phase(conn, cfg, transcriber=_backend_down)

    assert len(calls) == 1  # only the first note was ever attempted
    assert (outcome.done, outcome.failed, outcome.backend_down) == (0, 0, True)
    for _chat_id, msg_id, audio in notes:
        row = conn.execute(
            "SELECT transcription_status, media_path FROM messages WHERE source_message_id = ?",
            (msg_id,),
        ).fetchone()
        assert row["transcription_status"] == "pending"  # untouched, not flipped to failed
        assert row["media_path"] is not None and audio.exists()


def test_transcribe_file_raises_backend_down_on_connection_failure(tmp_path: Path) -> None:
    wav = tmp_path / "note.wav"
    wav.write_bytes(b"RIFF")

    class _UnreachableSession:
        def post(self, *_a: object, **_kw: object) -> object:
            import requests

            raise requests.ConnectionError("all connection attempts failed")

    with pytest.raises(tr.TranscriptionBackendDown):
        transcribe_file(wav, base_url="http://hub:8000", model="m", session=_UnreachableSession())  # type: ignore[arg-type]


def test_transcribe_file_raises_backend_down_on_gateway_error(tmp_path: Path) -> None:
    wav = tmp_path / "note.wav"
    wav.write_bytes(b"RIFF")

    class _GatewayErrorResp:
        status_code = 502
        text = '{"detail":"whisper upstream error: All connection attempts failed"}'

    class _GatewaySession:
        def post(self, *_a: object, **_kw: object) -> object:
            return _GatewayErrorResp()

    with pytest.raises(tr.TranscriptionBackendDown):
        transcribe_file(wav, base_url="http://hub:8000", model="m", session=_GatewaySession())  # type: ignore[arg-type]


def test_transcribe_file_still_isolates_per_file_400(tmp_path: Path) -> None:
    # A genuine per-file error (e.g. bad audio) is NOT a backend outage — stays a
    # plain TranscriptionError so the caller keeps isolating it per note.
    wav = tmp_path / "note.wav"
    wav.write_bytes(b"RIFF")

    class _BadRequestResp:
        status_code = 400
        text = '{"detail":"could not decode audio"}'

    class _BadRequestSession:
        def post(self, *_a: object, **_kw: object) -> object:
            return _BadRequestResp()

    with pytest.raises(TranscriptionError) as exc_info:
        transcribe_file(wav, base_url="http://hub:8000", model="m", session=_BadRequestSession())  # type: ignore[arg-type]
    assert not isinstance(exc_info.value, tr.TranscriptionBackendDown)


# --- review_monitored_chats gate (#132) ------------------------------------
#
# `wr review` (review_monitored_chats) must hold back an untranscribed voice
# note exactly like `scan`'s live delta does — otherwise the note is classified
# on its "[voice note]" placeholder and the cursor advances past it, so the
# real transcript produced by a later scan is never analysed.


def test_review_monitored_chats_holds_back_untranscribed_note(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    cfg = _config(tmp_path)  # transcription enabled
    chat_id, msg_id, audio = _monitor_with_voice(conn, cfg.linked_device_dir)

    outcome = review_monitored_chats(conn, StubClassifier(), config=cfg)

    # The only message is the held-back note — nothing to analyse yet.
    assert outcome.chats_with_delta == 0
    assert outcome.messages_processed == 0
    assert _baseline_id(conn, chat_id) is None  # cursor never advanced past it
    row = conn.execute(
        "SELECT transcription_status FROM messages WHERE source_message_id = ?", (msg_id,)
    ).fetchone()
    assert row["transcription_status"] == "pending"  # untouched, ready to retry


def test_review_monitored_chats_without_config_is_unchanged(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # Existing call sites (and most tests) don't pass `config` — the gate must
    # stay a no-op for them rather than force transcription awareness everywhere.
    cfg = _config(tmp_path)
    chat_id, _, _ = _monitor_with_voice(conn, cfg.linked_device_dir)

    outcome = review_monitored_chats(conn, StubClassifier())

    assert outcome.chats_with_delta == 1
    assert outcome.messages_processed == 1


def test_review_monitored_chats_disabled_transcription_is_unchanged(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    cfg = _config(tmp_path, enabled=False)
    chat_id, _, _ = _monitor_with_voice(conn, cfg.linked_device_dir)

    outcome = review_monitored_chats(conn, StubClassifier(), config=cfg)

    assert outcome.chats_with_delta == 1
    assert outcome.messages_processed == 1


# --- failed-note retry leash (#104) ----------------------------------------


def _chat(conn: sqlite3.Connection, sid: str = "c-retry") -> int:
    return store.upsert_chat(conn, ChatRecord(source_chat_id=sid, display_name="Class 4A"))


def test_failed_note_retried_beyond_transcribe_window(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # A note that already FAILED, older than window_days (7) but within
    # failed_retry_days (30): it is not backlog — its audio must survive the stale
    # sweep and it must be retried (the outage outlasted the transcribe window). #104
    cfg = _config(tmp_path, window_days=7, failed_retry_days=30)
    chat_id = _chat(conn)
    audio = _voice_note(
        conn, chat_id, msg_id="f14", buffer_dir=cfg.linked_device_dir,
        when=datetime.now(UTC) - timedelta(days=14), status="failed",
    )

    outcome = run_transcription_phase(
        conn, cfg, transcriber=lambda _p, _lang: "Please bring the form tomorrow"
    )

    assert (outcome.done, outcome.failed, outcome.skipped_old) == (1, 0, 0)
    row = conn.execute(
        "SELECT text, transcription_status, media_path FROM messages "
        "WHERE source_message_id = 'f14'"
    ).fetchone()
    assert row["transcription_status"] == "done"
    assert row["text"] == "Please bring the form tomorrow"
    assert row["media_path"] is not None and audio.exists()  # not swept before retry


def test_failed_note_given_up_past_failed_retry_window(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # A failed note older than failed_retry_days (30): the outage never recovered, so
    # we give up — skip it and delete its audio (bounded retention of sensitive data).
    cfg = _config(tmp_path, window_days=7, failed_retry_days=30)
    chat_id = _chat(conn)
    audio = _voice_note(
        conn, chat_id, msg_id="f40", buffer_dir=cfg.linked_device_dir,
        when=datetime.now(UTC) - timedelta(days=40), status="failed",
    )
    calls: list[Path] = []

    outcome = run_transcription_phase(
        conn, cfg, transcriber=lambda p, _lang: calls.append(p) or "never"
    )

    assert calls == []  # past the leash → never retried
    assert (outcome.done, outcome.skipped_old) == (0, 1)
    row = conn.execute(
        "SELECT transcription_status, media_path FROM messages WHERE source_message_id = 'f40'"
    ).fetchone()
    assert row["transcription_status"] == "skipped_old"
    assert row["media_path"] is None and not audio.exists()


def test_pending_backlog_still_gated_by_window_not_failed_leash(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # The longer leash is ONLY for 'failed' notes. A never-attempted 'pending' note
    # older than window_days (7) — even within failed_retry_days (30) — is still
    # skipped as first-pairing backlog, never transcribed. Guards the status split.
    cfg = _config(tmp_path, window_days=7, failed_retry_days=30)
    chat_id = _chat(conn)
    audio = _voice_note(
        conn, chat_id, msg_id="p14", buffer_dir=cfg.linked_device_dir,
        when=datetime.now(UTC) - timedelta(days=14), status="pending",
    )
    calls: list[Path] = []

    outcome = run_transcription_phase(
        conn, cfg, transcriber=lambda p, _lang: calls.append(p) or "never"
    )

    assert calls == []  # pending backlog past the window is not retried
    assert (outcome.done, outcome.skipped_old) == (0, 1)
    row = conn.execute(
        "SELECT transcription_status, media_path FROM messages WHERE source_message_id = 'p14'"
    ).fetchone()
    assert row["transcription_status"] == "skipped_old"
    assert row["media_path"] is None and not audio.exists()


# --- per-chat language inference (#36) -------------------------------------


def _add_text(conn: sqlite3.Connection, chat_id: int, prefix: str, body: str, n: int = 1) -> None:
    store.insert_messages(
        conn,
        chat_id,
        [
            MessageRecord(
                source_message_id=f"{prefix}{i}",
                message_timestamp=datetime.now(UTC).isoformat(),
                text=body,
                message_type="text",
            )
            for i in range(n)
        ],
    )


def test_phase_infers_language_from_chat_text(conn: sqlite3.Connection, tmp_path: Path) -> None:
    cfg = _config(tmp_path)  # language='auto' → infer per chat
    chat_id, _, _ = _monitor_with_voice(conn, cfg.linked_device_dir)
    _add_text(conn, chat_id, "es", "Hola mami, ¿cómo estás? Te quiero mucho, prueba bonita.")

    captured: dict[str, object] = {}

    def _t(_p: Path, language: str | None) -> str:
        captured["lang"] = language
        return "transcripción"

    run_transcription_phase(conn, cfg, transcriber=_t)
    assert captured["lang"] == "es"  # detected from the chat's Spanish text


def test_explicit_language_pin_overrides_detection(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    cfg = _config(tmp_path, language="en")  # explicit pin wins over any detection
    chat_id, _, _ = _monitor_with_voice(conn, cfg.linked_device_dir)
    _add_text(conn, chat_id, "es", "Hola mami, ¿cómo estás? Te quiero mucho, prueba bonita.")

    captured: dict[str, object] = {}

    def _t(_p: Path, language: str | None) -> str:
        captured["lang"] = language
        return "x"

    run_transcription_phase(conn, cfg, transcriber=_t)
    assert captured["lang"] == "en"


def test_no_hint_when_chat_has_too_little_text(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    cfg = _config(tmp_path)
    # _monitor_with_voice's chat has no text at all → detection returns None.
    _monitor_with_voice(conn, cfg.linked_device_dir)

    captured: dict[str, object] = {"lang": "unset"}

    def _t(_p: Path, language: str | None) -> str:
        captured["lang"] = language
        return "x"

    run_transcription_phase(conn, cfg, transcriber=_t)
    assert captured["lang"] is None  # nothing to detect from → backend auto-detect


# --- transcribe_file: hub call shape + WAV transcode (offline, mocked) -----


class _FakeResp:
    def __init__(self, payload: dict[str, object]) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> _FakeResp:
        self.calls.append({"url": url, **kwargs})
        return _FakeResp({"text": "  hello   world \n"})


def test_transcribe_file_posts_wav_directly_and_flattens(tmp_path: Path) -> None:
    wav = tmp_path / "note.wav"
    wav.write_bytes(b"RIFF....WAVE")
    sess = _FakeSession()

    text = transcribe_file(
        wav, base_url="http://hub:8000/", model="whisper-vanilla", language="auto", session=sess  # type: ignore[arg-type]
    )

    assert text == "hello world"  # whitespace runs flattened
    call = sess.calls[0]
    assert call["url"] == "http://hub:8000/v1/audio/transcriptions"
    # No language hint → whisper-vanilla auto-detects the source language server-side.
    assert call["data"] == {"model": "whisper-vanilla", "response_format": "json"}
    name, content, mime = call["files"]["file"]  # type: ignore[index]
    assert name == "note.wav" and content == b"RIFF....WAVE" and mime == "audio/wav"


def test_transcribe_file_sends_language_when_pinned(tmp_path: Path) -> None:
    wav = tmp_path / "note.wav"
    wav.write_bytes(b"RIFF")
    sess = _FakeSession()
    transcribe_file(wav, base_url="http://hub:8000", model="m", language="es", session=sess)  # type: ignore[arg-type]
    assert sess.calls[0]["data"] == {"model": "m", "response_format": "json", "language": "es"}


def test_transcribe_file_transcodes_non_wav_with_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ogg = tmp_path / "note.ogg"
    ogg.write_bytes(b"OggS-fake")
    sess = _FakeSession()
    ran: dict[str, object] = {}

    monkeypatch.setattr(tr.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    class _Proc:
        stdout = b"RIFF-transcoded-wav"

    def _fake_run(cmd: list[str], **kw: object) -> _Proc:
        ran["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(tr.subprocess, "run", _fake_run)

    transcribe_file(ogg, base_url="http://hub:8000", model="m", session=sess)  # type: ignore[arg-type]

    # ffmpeg invoked to make 16 kHz mono PCM WAV; the transcoded bytes (not the ogg)
    # are what gets POSTed, under a .wav filename.
    assert "ffmpeg" in str(ran["cmd"][0]) and "16000" in ran["cmd"]  # type: ignore[index]
    name, content, mime = sess.calls[0]["files"]["file"]  # type: ignore[index]
    assert name == "note.wav" and content == b"RIFF-transcoded-wav" and mime == "audio/wav"


def test_transcribe_file_errors_clearly_without_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ogg = tmp_path / "note.ogg"
    ogg.write_bytes(b"OggS")
    monkeypatch.setattr(tr.shutil, "which", lambda _name: None)
    with pytest.raises(TranscriptionError, match="ffmpeg"):
        transcribe_file(ogg, base_url="http://hub:8000", model="m")


# --- glossary-free transcription default (#88) -----------------------------


def test_default_model_is_glossary_free_auto_detect() -> None:
    """The shipped default must be ``whisper-vanilla`` so non-English notes
    auto-detect their source language instead of being Englishized by the plain
    turbo's English glossary (local-llm-hub#128 / #88). Asserts both the dataclass
    default and the committed ``config/default.json``."""
    assert TranscriptionConfig().model == "whisper-vanilla"
    assert TranscriptionConfig().language == "auto"  # send none → auto-detect

    default_json = Path(__file__).resolve().parents[1] / "config" / "default.json"
    tr_cfg = json.loads(default_json.read_text(encoding="utf-8"))["transcription"]
    assert tr_cfg["model"] == "whisper-vanilla"
    assert tr_cfg["language"] == "auto"


# --- helpers ---------------------------------------------------------------


def _baseline_id(conn: sqlite3.Connection, chat_id: int) -> int | None:
    row = conn.execute(
        "SELECT last_processed_message_id FROM chat_review_state WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    return row["last_processed_message_id"] if row else None


def _scan_with_transcriber(conn: sqlite3.Connection, cfg: Config, transcriber: object):
    """Run a live scan whose transcription phase uses an injected transcriber.

    The scan builds its own phase internally, so we monkeypatch the module-level
    phase to thread the fake transcriber through — keeping the test offline while
    exercising the real ordering (transcribe → analyse) inside ``scan``.
    """
    import src.analysis.pipeline as pipeline
    from src.analysis import transcription as tr

    real_phase = tr.run_transcription_phase

    def _phase(c: sqlite3.Connection, config: Config, **kw: object):
        kw.pop("transcriber", None)
        return real_phase(c, config, transcriber=transcriber, **kw)  # type: ignore[arg-type]

    original = pipeline.run_transcription_phase
    pipeline.run_transcription_phase = _phase  # type: ignore[assignment]
    try:
        return scan(
            conn, cfg, mode="live", connector=_StaticConnector(), classifier=StubClassifier()
        )
    finally:
        pipeline.run_transcription_phase = original  # type: ignore[assignment]


class _StaticConnector:
    """A live connector that adds no new chats/messages (the store already holds them)."""

    def connect(self) -> object:
        from src.connector.base import ConnectorStatus

        return ConnectorStatus(name="static", connected=True, detail="ok")

    def status(self) -> object:
        return self.connect()

    def list_chats(self) -> list[ChatRecord]:
        return []

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        return []

    def canonical_source_id(self, source_chat_id: str) -> str | None:
        return source_chat_id

    def stop(self) -> None:
        return None
