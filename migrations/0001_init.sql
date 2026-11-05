-- onhold's whole database: drafts waiting for the owner's go-ahead, and one
-- row per call with its report. Times are Unix seconds (UTC).

CREATE TABLE drafts (
    id           TEXT PRIMARY KEY,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    brief_json   TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    -- Set once, atomically, when the draft is used for a call.
    used_at      INTEGER,
    call_id      TEXT
);

CREATE TABLE calls (
    id                  TEXT PRIMARY KEY,
    draft_id            TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    mode                TEXT NOT NULL CHECK (mode IN ('now', 'scheduled')),
    callee_name         TEXT NOT NULL,
    to_number           TEXT NOT NULL,
    topic               TEXT NOT NULL,
    reference           TEXT,
    brief_json          TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN
                            ('scheduled', 'dialing', 'in_progress', 'ended', 'failed', 'cancelled')),
    outcome             TEXT CHECK (outcome IN ('goal_met', 'partial', 'needs_owner', 'not_reached')),
    reason              TEXT,
    summary             TEXT,
    answers_json        TEXT,
    follow_ups_json     TEXT,
    details_json        TEXT,
    shared_json         TEXT,
    evaluation_json     TEXT,
    conversation_id     TEXT,
    batch_id            TEXT,
    scheduled_for       INTEGER,
    started_at          INTEGER,
    ended_at            INTEGER,
    duration_secs       INTEGER,
    cost                INTEGER,
    transcript_json     TEXT,
    transcript_purge_at INTEGER,
    notified_at         INTEGER
);

CREATE UNIQUE INDEX ix_calls_conversation ON calls (conversation_id);
CREATE INDEX ix_calls_created ON calls (created_at);
CREATE INDEX ix_calls_status ON calls (status);
CREATE INDEX ix_calls_reference ON calls (reference);
