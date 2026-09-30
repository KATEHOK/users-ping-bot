-- personal command menus (member scope) the bot has set: where to delete them on a role change
-- no FK to chats: groups the bot was added to but nobody registered are not stored there
CREATE TABLE member_menus (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('owner', 'register')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX member_menus_user_id ON member_menus(user_id);
