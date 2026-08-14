-- GameWatch schema. Applied automatically on startup by db.init(); kept here
-- so you can read it, diff it, or run it by hand against a fresh database.

CREATE TABLE IF NOT EXISTS chats (
    chat_id      BIGINT PRIMARY KEY,
    title        TEXT,
    digest_day   SMALLINT NOT NULL DEFAULT 0,   -- 0 = Monday, matches date.weekday()
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (chat, game). The release_* columns are a SNAPSHOT of what we
-- last told the user, which is what makes change detection possible: compare
-- the live source against this, not against another live call.
CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id           BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    game_key          TEXT   NOT NULL,
    name              TEXT   NOT NULL,
    release_date      DATE,
    release_human     TEXT   NOT NULL DEFAULT 'TBD',
    platforms         TEXT   NOT NULL DEFAULT '',
    url               TEXT   NOT NULL DEFAULT '',
    released_notified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, game_key)
);

CREATE INDEX IF NOT EXISTS subscriptions_game_key_idx ON subscriptions (game_key);
CREATE INDEX IF NOT EXISTS subscriptions_release_idx  ON subscriptions (release_date)
    WHERE released_notified = FALSE;

-- Discovery rules: "tell me about new RPGs", "anything from FromSoftware".
CREATE TABLE IF NOT EXISTS discovery_rules (
    chat_id    BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    kind       TEXT   NOT NULL CHECK (kind IN ('genre', 'platform', 'company')),
    value_id   TEXT   NOT NULL,
    label      TEXT   NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, kind, value_id)
);

-- Games already surfaced by discovery, so a user hears about each game once.
CREATE TABLE IF NOT EXISTS discovery_seen (
    chat_id   BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    game_key  TEXT   NOT NULL,
    seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, game_key)
);

-- Bookkeeping so a retried cron run can't double-send, and so /status can
-- report when checks last succeeded.
CREATE TABLE IF NOT EXISTS job_runs (
    job         TEXT PRIMARY KEY,
    last_run_at TIMESTAMPTZ,
    last_status TEXT,
    detail      TEXT
);
