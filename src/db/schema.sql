-- WhatsApp Radar local state. All runtime databases live under ignored paths.
-- Concepts are kept deliberately separate: source messages, per-chat review
-- cursor, LLM analysis result, and notification delivery state.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Discovered chats with sanitized metadata. status: 'discovered' | 'monitored' | 'ignored'.
CREATE TABLE IF NOT EXISTS chats (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Which connector a chat came from: 'whatsapp' (default) or a future source
    -- (e.g. 'gmail', #46). Chat identity is (source, source_chat_id) so a Gmail
    -- sender/label id can't collide with a WhatsApp JID. Messages stay source-less
    -- and inherit their chat's source.
    source                   TEXT NOT NULL DEFAULT 'whatsapp',
    source_chat_id           TEXT NOT NULL,
    display_name             TEXT NOT NULL,
    -- Operator-supplied label that overrides display_name in the UI. The human
    -- fallback for chats the connector can only name as a bare number/JID.
    alias                    TEXT,
    chat_type                TEXT NOT NULL DEFAULT 'group',
    status                   TEXT NOT NULL DEFAULT 'discovered',
    first_seen_at            TEXT NOT NULL,
    last_seen_at             TEXT NOT NULL,
    last_message_at          TEXT,
    monitor_frequency_minutes INTEGER,
    -- Operator-declared link: when set, this chat is a *child* folded into the
    -- parent (a top-level chat) so the same person reached under two numbers is
    -- one family. Depth is capped at 1 (a child can't itself be a parent) by the
    -- link API. Pure metadata over the per-chat rows — no message data moves, and
    -- each chat keeps its own cursor. ON DELETE SET NULL so dropping a parent
    -- frees its children rather than cascading.
    parent_chat_id           INTEGER REFERENCES chats(id) ON DELETE SET NULL,
    UNIQUE (source, source_chat_id)
);

-- Raw-but-local message records. Idempotent on (chat_id, source_message_id).
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id           INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    source_message_id TEXT NOT NULL,
    sender_label      TEXT,
    message_timestamp TEXT NOT NULL,
    text              TEXT,
    message_type      TEXT NOT NULL DEFAULT 'text',
    -- Voice-note transcription (#36). Lifecycle for a voice note's audio:
    --   'pending'      audio downloaded by the sidecar, awaiting transcription
    --   'done'         transcribed; `text` now holds the transcript (placeholder
    --                  preserved in raw_json); audio retained for playback until the
    --                  retention sweep, then deleted (#86)
    --   'failed'       transcription errored — retried on the next live scan
    --   'skipped_old'  voice note older than the transcription window; never fetched
    -- NULL for every non-voice message, so the analysis pipeline (which reads only
    -- `text`) is untouched and a transcript flows through exactly like typed text.
    transcription_status TEXT,
    -- Relative path (under the linked-device buffer dir) to the downloaded voice
    -- audio. Set while awaiting transcription and, with retention on (#86), kept
    -- pointing at the retained file so it can be played back; cleared when the audio
    -- is swept past `transcription.audio_retention_days` (or immediately on success
    -- when retention is 0).
    media_path        TEXT,
    -- On-demand summary text (#157), persisted the first time the operator taps
    -- Summarize so a reopened overlay never re-pays the hub call. NULL until
    -- requested; cleared whenever `text` is replaced (voice-note retranscription)
    -- so a stale summary can never be shown or spoken.
    summary           TEXT,
    raw_json          TEXT,
    ingested_at       TEXT NOT NULL,
    UNIQUE (chat_id, source_message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_order
    ON messages (chat_id, message_timestamp, id);

-- Per-chat review cursor. Advanced ONLY after analysis state is persisted.
CREATE TABLE IF NOT EXISTS chat_review_state (
    chat_id                        INTEGER PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
    last_reviewed_at               TEXT,
    last_processed_message_id      INTEGER REFERENCES messages(id),
    last_processed_message_timestamp TEXT,
    rolling_context_json           TEXT
);

-- One row per scan/review run. Beyond lifecycle (status/timing) it records the
-- run mode, its parameters, and the full funnel so any run is inspectable:
-- how much was synced, how much passed each stage, and what was delivered.
CREATE TABLE IF NOT EXISTS review_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    mode                TEXT NOT NULL DEFAULT 'review',  -- 'live' | 'dry_run' | 'review'
    params_json         TEXT,                            -- run parameters (e.g. {"days": 7})
    chats_reviewed      INTEGER NOT NULL DEFAULT 0,      -- chats that had a delta
    chats_synced        INTEGER NOT NULL DEFAULT 0,      -- chats pulled from the connector (live)
    messages_synced     INTEGER NOT NULL DEFAULT 0,      -- new messages stored (live)
    chats_monitored     INTEGER NOT NULL DEFAULT 0,      -- monitored chats considered
    stage1_passed       INTEGER NOT NULL DEFAULT 0,      -- deltas that passed the keyword prefilter
    stage2_llm_calls    INTEGER NOT NULL DEFAULT 0,      -- LLM classifications actually made
    transcriptions      INTEGER NOT NULL DEFAULT 0,      -- voice notes transcribed this run (#36)
    actionable          INTEGER NOT NULL DEFAULT 0,      -- chats with an actionable verdict
    notification_status TEXT,                            -- 'sent'|'failed'|'skipped'|'dry_run'|'none'
    source_funnel_json  TEXT,                            -- truthful per-source counters/status
    error               TEXT
);

-- Per-run, per-chat audit trail: the complete decision record for one chat in one
-- run. A missed important message is a real failure, so every stage is captured —
-- the analyzed delta, the keyword evidence, the exact LLM prompt and raw response,
-- the validated result, the final action, and the digest text it contributed.
CREATE TABLE IF NOT EXISTS analysis_trace (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                 INTEGER NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    chat_id                INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    input_message_ids_json TEXT,            -- source_message_ids analyzed
    input_text             TEXT,            -- rendered delta handed to stage 1
    messages_json          TEXT,            -- per-message record: [{id,sender,text,roots}] (#12)
    stage1_passed          INTEGER NOT NULL DEFAULT 0,
    stage1_roots_json      TEXT,            -- keyword roots that triggered (Stage-1 evidence)
    stage1_buckets_json    TEXT,            -- source-specific taxonomy buckets matched
    llm_called             INTEGER NOT NULL DEFAULT 0,
    llm_system_prompt      TEXT,            -- exact system prompt sent (Stage 2)
    llm_user_prompt        TEXT,            -- exact user prompt sent (Stage 2)
    llm_raw_response       TEXT,            -- raw model text before JSON extraction
    parsed_result_json     TEXT,            -- validated AnalysisResult (null on contract error)
    final_action           TEXT NOT NULL,   -- 'not_actionable' | 'actionable' | 'contract_error' | 'llm_truncated'
    telegram_text          TEXT,            -- this chat's contribution to the digest
    error                  TEXT,            -- contract error detail, if any
    created_at             TEXT NOT NULL,
    UNIQUE (run_id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_analysis_trace_run ON analysis_trace (run_id);

CREATE TABLE IF NOT EXISTS analysis_items (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                    INTEGER NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    chat_id                   INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    action_required           INTEGER NOT NULL,
    priority                  TEXT,
    summary                   TEXT,
    suggested_next_action     TEXT,
    deadline                  TEXT,            -- free-text date/time as stated (prose)
    deadline_date             TEXT,            -- model-resolved absolute date 'YYYY-MM-DD' (#71)
    confidence                REAL,
    evidence_message_ids_json TEXT,
    created_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    channel  TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'pending',
    sent_at  TEXT,
    error    TEXT
);

-- One row per sync (ingest) so the operator can see, at a glance, that syncing is
-- pulling new data: when it ran, how many chats/messages it added, and the running
-- totals afterwards. Written by every sync path (resync + live scan + the resync
-- a reprocess wraps) so a scheduled job is as visible as a webapp click.
-- Per-message ingest time lives on messages.ingested_at; this is the per-run
-- summary on top of it.
CREATE TABLE IF NOT EXISTS sync_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at         TEXT NOT NULL,
    source         TEXT NOT NULL,                  -- 'resync' | 'scan' | 'reprocess'
    connector_source TEXT NOT NULL DEFAULT 'whatsapp',
    status         TEXT NOT NULL DEFAULT 'success',
    detail         TEXT NOT NULL DEFAULT '',
    chats_added    INTEGER NOT NULL DEFAULT 0,
    chats_updated  INTEGER NOT NULL DEFAULT 0,
    messages_added INTEGER NOT NULL DEFAULT 0,
    total_chats    INTEGER NOT NULL DEFAULT 0,
    total_messages INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sync_log_ran ON sync_log (ran_at DESC);
