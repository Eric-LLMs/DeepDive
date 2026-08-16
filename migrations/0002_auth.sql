-- 0002_auth: multi-user auth + server-managed settings.
--
-- Adds login columns to the users table and a key/value settings table that the admin
-- console reads/writes (LLM provider config, admin credential, user tiers). Idempotent
-- so it is safe to re-run against an existing database.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS username      TEXT,
    ADD COLUMN IF NOT EXISTS password_hash TEXT,
    ADD COLUMN IF NOT EXISTS display_name  TEXT,
    ADD COLUMN IF NOT EXISTS is_active     BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS tier          TEXT NOT NULL DEFAULT 'regular';

-- Unique on non-null usernames; legacy anonymous rows (NULL username) are unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS users_username_idx
    ON users (username) WHERE username IS NOT NULL;

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
