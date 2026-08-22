-- Session auto-title: short LLM-generated title from the first user message.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title text;
