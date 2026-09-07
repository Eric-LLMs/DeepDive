-- Session type: 0 = ordinary chat, 1 = research task session. Research sessions are hidden
-- from the chat sidebar (kind lives in the DB now, not only in _session_index.json) and are
-- deleted together with their task.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS type integer NOT NULL DEFAULT 0;
