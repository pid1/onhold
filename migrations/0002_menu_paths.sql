-- Phone-tree memory: the keypad path that got a call to a person, per number
-- called. Learned from the keypad tones the agent pressed (never from what the
-- menu said), offered to the brief writer, and passed to the agent only when a
-- brief includes it. Facts about the office, never about the owner.

CREATE TABLE menu_paths (
    to_number      TEXT NOT NULL,
    -- The presses in order, comma-separated: "2,4". Each is 1-4 of 0-9 * #.
    digits         TEXT NOT NULL,
    -- [{"press": "2", "option": "billing"}]; option is the agent's own note.
    steps_json     TEXT NOT NULL,
    -- The topic of the last call it worked for.
    topic          TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    worked_count   INTEGER NOT NULL DEFAULT 0,
    last_worked_at INTEGER,
    last_call_id   TEXT,
    failed_count   INTEGER NOT NULL DEFAULT 0,
    last_failed_at INTEGER,
    PRIMARY KEY (to_number, digits)
);
