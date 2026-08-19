-- 0002_auth_profiles.sql: self-service registration + profile fields.
--
-- Adds contact/profile columns to users (email/phone/avatar) plus an email-verification
-- gate, and a one-time token table for email verification + password reset.
--
-- Idempotent: statements are IF NOT EXISTS / guarded, so re-running is safe.


-- 0002a: user profile columns + verified gate.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email          TEXT,
    ADD COLUMN IF NOT EXISTS phone          TEXT,
    ADD COLUMN IF NOT EXISTS avatar         TEXT,
    ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false;

CREATE UNIQUE INDEX IF NOT EXISTS users_email_idx
    ON users (email) WHERE email IS NOT NULL;


-- 0002b: one-time verification / reset tokens (hash stored; raw value shown once).
CREATE TABLE IF NOT EXISTS verification_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                      -- 'verify' | 'reset'
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
