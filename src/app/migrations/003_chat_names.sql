-- per-chat display name of a member (used in pings and lists instead of the Telegram name)
-- the cascade drops names with the registration, by any removal path
CREATE TABLE chat_names (
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
