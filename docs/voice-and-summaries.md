# Voice notes and summaries

Two opt-in features that run through the local LLM Hub: transcribing voice notes, and on-demand summaries that can be read aloud.

## Voice-note transcription

With transcription enabled, the sidecar downloads each voice note's audio to the ignored `data/linked_device/media/`, and a transcription phase in every live `scan` (between sync and analysis) sends it to the local LLM Hub's Whisper endpoint. Without it, voice notes flow through as the literal text `[voice note]` and anything spoken rather than typed is silently missed.

Transcribed audio is **retained for playback** for `audio_retention_days`: in the Chats overlay the 🎤 marker becomes a tap-to-play/stop control that streams the note from an authenticated, read-only endpoint (`GET /api/messages/{id}/audio`, gated by the same auth as the rest of the API; the `<audio>` element passes the token via `?token=`, and loopback bypasses). WhatsApp voice notes are OGG/Opus, which iOS Safari cannot play in an `<audio>` element, so the endpoint **transcodes to MP3 on the fly** with ffmpeg for universal playback, falling back to the original bytes if ffmpeg is unavailable. The control appears on any voice note whose audio is still on disk, so a note can be played back even before — or if — its transcription completes. A sweep at the start of each transcription phase deletes audio past the window and clears its `media_path`, after which the control disappears and the endpoint 404s; the transcript is kept either way. Audio is more sensitive than text, so the window is short by default and the files never leave the gitignored buffer dir. Set `audio_retention_days: 0` to delete the audio immediately on a successful transcription.

It is **off by default** and opt-in, routing through the hub directly — no extra dependency, no detour through voice-transcriber. Configure it under `transcription` in `config/default.json` (override per-host in `config/local.json`, or via `WR_TRANSCRIPTION_*` env):

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Master switch; `false` makes the phase a no-op (voice notes stay `[voice note]`). |
| `window_days` | `7` | Only *never-attempted* voice notes from the last N days are transcribed; older ones are marked `skipped_old` and never fetched, so a fresh pairing never transcribes years of backlog. Notes that already *failed* get the longer `failed_retry_days` leash instead. |
| `failed_retry_days` | `30` | How long a note that already *failed* keeps being retried (and its audio kept) before giving up. A failure means a transient outage, not backlog, so it retries on **every** full sync regardless of `window_days` — bounded here so a multi-day outage recovers without keeping sensitive audio forever. |
| `audio_base_url` | `http://127.0.0.1:8000` | The hub's audio base URL (its `:8000` proxy keeps the call in the hub's observability ring); `/v1/audio/transcriptions` is appended. |
| `model` | `whisper-vanilla` | OpenAI-shape model id sent in the multipart form. `whisper-vanilla` is the hub's glossary-free turbo path that auto-detects the source language. Do **not** use the plain turbo (`whisper-1`) — it carries an English glossary and defaults to `en`, Englishizing non-English notes. |
| `language` | `auto` | `auto` infers each chat's language from its own text (chats are single-language) and passes it as the Whisper hint, so a note transcribes in its real language regardless of any backend auto-detect bias. Falls back to the backend's auto-detect when a chat has too little text. Pin to an ISO code (e.g. `es`) to force one language for every note. |
| `timeout_seconds` | `120` | Per-file transcription request timeout. |
| `audio_retention_days` | `7` | Days a transcribed note's audio is kept on disk for playback before the sweep deletes it. `0` deletes the audio immediately on success (no playback). |

Transcribe-only, never translation. Failures are isolated and **retried on every full sync** up to `failed_retry_days`: a voice note whose transcription errors is held back from analysis and the cursor never advances past it, so its real transcript is never skipped, while analysis of the other chats proceeds. Because a failed note keeps its audio and retries regardless of `window_days`, a backend outage lasting longer than the transcribe window still recovers the whole backlog once the backend is back.

When the **whole** whisper backend is unreachable — connection refused, or a `502`/`503`/`504` gateway error, as opposed to one bad file — the batch short-circuits after the first note: one `whisper backend unreachable` line instead of one warning per pending note, and every remaining note is left untouched (still `pending` / `failed`, not flipped) for the next scan to retry.

> **Requires `ffmpeg` on PATH.** WhatsApp voice notes are OGG/Opus, but the hub's whisper backend only decodes WAV, so the transcription client transcodes each note to 16 kHz mono WAV with ffmpeg before sending. Without ffmpeg, transcription fails with a clear error (and the note is retried); analysis is unaffected.

**Why the language hint exists.** The hub's shared turbo whisper-server carries an English tech-dictation glossary as its initial prompt, which biases pure audio auto-detect toward English — a Spanish note comes back Englishized. Rather than personalize anything app-side, the app passes the correct standard `language` hint per note. A plain-vanilla, glossary-free transcription option in the hub, reusable by any caller, is tracked in [`ferraroroberto/local-llm-hub#128`](https://github.com/ferraroroberto/local-llm-hub/issues/128); once it lands, `language: auto` can rely on unbiased audio detection directly.

## On-demand summaries and read-aloud

A long message in the Chats overlay — a long voice-note transcript or a long typed message — shows a **Summarize** control. It is strictly on-demand (ingest, sync, and scan never generate a summary) and the result is **persisted** to `messages.summary`, an additive nullable column: the first tap dials the hub's `claude_haiku`; every later tap, page reload, or overlay reopening returns the stored text with no further hub call. Retranscribing a voice note clears its stored summary automatically (`store.mark_transcription`), so a retranscription can never leave a stale summary visible or spoken.

**Play summary aloud** streams the summary through one of four logical voice profiles — `en_female` / `en_male` / `es_female` / `es_male` — resolved entirely server-side from the message's own context, never from the client:

- **Language** is detected deterministically from the *original* message text (never the summary), using the same `langdetect` pattern the transcription phase uses for its Whisper hint. Below ~20 characters of text, a detector error, or any language other than Spanish all fall back to English — the feature only distinguishes English and Spanish.
- **Gender** comes from an explicit per-sender mapping (`sender_voice_genders` in `config/webapp_config.json`, keyed by the lowercased/trimmed sender label) with a configured fallback (`default_voice_gender`, default `"female"`) for any unmapped or unlabeled sender. It is never inferred from a name — only an explicit mapping counts. Edit the JSON directly; there is no UI form for either key.

The hub model and voice behind each profile are committed, non-secret config under `tts.profiles` in `config/default.json` (override per-host in `config/local.json`):

| Profile | Default model | Default voice |
| --- | --- | --- |
| `en_female` | `orpheus-tts` | `tara` |
| `en_male` | `orpheus-tts` | `leo` |
| `es_female` | `kokoro-tts` | `ef_dora` |
| `es_male` | `kokoro-tts` | `em_alex` |

A resolved voice's own backend being unavailable (e.g. `kokoro-tts` not loaded on the hub) surfaces as `503`, distinct from a general hub outage (`502`), so the two failure modes are never confused in logs or the UI. Summary **text** is persisted; synthesized **audio** stays streamed and ephemeral — never written to disk, never retained.
