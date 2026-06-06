-- WhatsApp Radar local state. All runtime databases live under ignored paths.
-- Concepts are kept deliberately separate: source messages, per-chat review
-- cursor, LLM analysis result, and notification delivery state.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Discovered chats with sanitized metadata. status: 'discovered' | 'monitored' | 'ignored'.
CREATE TABLE IF NOT EXISTS chats (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_chat_id           TEXT NOT NULL UNIQUE,
    display_name             TEXT NOT NULL,
    chat_type                TEXT NOT NULL DEFAULT 'group',
    status                   TEXT NOT NULL DEFAULT 'discovered',
    first_seen_at            TEXT NOT NULL,
    last_seen_at             TEXT NOT NULL,
    last_message_at          TEXT,
    monitor_frequency_minutes INTEGER
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
    actionable          INTEGER NOT NULL DEFAULT 0,      -- chats with an actionable verdict
    notification_status TEXT,                            -- 'sent'|'failed'|'skipped'|'dry_run'|'none'
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
    stage1_passed          INTEGER NOT NULL DEFAULT 0,
    stage1_roots_json      TEXT,            -- keyword roots that triggered (Stage-1 evidence)
    llm_called             INTEGER NOT NULL DEFAULT 0,
    llm_system_prompt      TEXT,            -- exact system prompt sent (Stage 2)
    llm_user_prompt        TEXT,            -- exact user prompt sent (Stage 2)
    llm_raw_response       TEXT,            -- raw model text before JSON extraction
    parsed_result_json     TEXT,            -- validated AnalysisResult (null on contract error)
    final_action           TEXT NOT NULL,   -- 'not_actionable' | 'actionable' | 'contract_error'
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
    deadline                  TEXT,
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
