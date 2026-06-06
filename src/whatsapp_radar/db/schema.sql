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

CREATE TABLE IF NOT EXISTS review_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    completed_at   TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    chats_reviewed INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);

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
