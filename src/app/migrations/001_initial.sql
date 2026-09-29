CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    private_contact_at TEXT,
    lang TEXT NOT NULL DEFAULT 'en',
    updated_at TEXT NOT NULL
);

CREATE TABLE roles (
    user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('root', 'admin')),
    granted_at TEXT NOT NULL
);

CREATE UNIQUE INDEX roles_single_root ON roles(role) WHERE role = 'root';

CREATE TABLE chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    registered_by INTEGER NOT NULL REFERENCES users(user_id),
    registered_at TEXT NOT NULL,
    registration_generation INTEGER NOT NULL,
    lang TEXT NOT NULL DEFAULT 'en'
);

CREATE TABLE subscriptions (
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

-- no FK to chats: outbox events outlive chat registrations
CREATE TABLE outbox (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('chat', 'user')),
    target_id INTEGER NOT NULL,
    generation INTEGER,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'cancelled', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT
);

CREATE TABLE processed_updates (
    bot_id INTEGER NOT NULL,
    update_id INTEGER NOT NULL,
    processed_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    PRIMARY KEY (bot_id, update_id)
);

CREATE INDEX processed_updates_processed_at ON processed_updates(processed_at);

-- no FK to chats: aliases must survive the old chat row being dropped
CREATE TABLE chat_aliases (
    old_chat_id INTEGER PRIMARY KEY,
    new_chat_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

INSERT INTO counters (name, value) VALUES ('generation', 0);
