-- 0008_login_tokens: split access_tokens into two concerns.
--
-- access_tokens was doing double duty: it was the login/API credential *and* the per-user
-- LLM-key permission record, so the single is_active column conflated "can this user sign in"
-- with "may this user use this key" — revoking an expired credential silently killed the key
-- too. This migration separates them:
--   * login_tokens  — the login/API credential (token_hash, role, expiry, the key pinned at
--     login). Its is_active is credential validity only.
--   * access_tokens — the per-user LLM-key grant matrix (user × key, is_active = key ban).
--     All login data is stripped out entirely.
--
-- Old access_tokens rows are copied into login_tokens preserving the same id, so the
-- user_usage_logs.token_id FK keeps resolving; it is then re-pointed to login_tokens.

-- ── 1. Login-credential table (same shape as the old access_tokens) ──
CREATE TABLE IF NOT EXISTS login_tokens (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID REFERENCES users (id) ON DELETE CASCADE,   -- NULL = admin/API token
    name          TEXT NOT NULL,                                  -- human label
    token_hash    TEXT NOT NULL UNIQUE,                           -- sha256(raw token)
    role          TEXT NOT NULL DEFAULT 'user',                   -- 'admin' | 'user'
    role_id       TEXT REFERENCES user_roles (role_id) ON DELETE SET NULL,
    credential_id UUID REFERENCES llm_credentials (id) ON DELETE SET NULL,  -- key pinned at login
    expires_at    TIMESTAMPTZ,
    last_used_at  TIMESTAMPTZ,
    is_active     BOOLEAN NOT NULL DEFAULT true,                  -- login-credential validity
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_tokens_user_id_idx ON login_tokens (user_id);
CREATE INDEX IF NOT EXISTS login_tokens_credential_id_idx ON login_tokens (credential_id);

-- ── 2. Copy every access_tokens row (same ids) into login_tokens ──
INSERT INTO login_tokens (id, user_id, name, token_hash, role, role_id, credential_id,
                          expires_at, last_used_at, is_active, created_at)
SELECT id, user_id, name, token_hash, role, role_id, credential_id,
       expires_at, last_used_at, is_active, created_at
FROM access_tokens;

-- ── 3. Re-point the usage-log FK from access_tokens → login_tokens ──
ALTER TABLE user_usage_logs DROP CONSTRAINT IF EXISTS user_usage_logs_token_id_fkey;
ALTER TABLE user_usage_logs ADD CONSTRAINT user_usage_logs_token_id_fkey
    FOREIGN KEY (token_id) REFERENCES login_tokens (id) ON DELETE SET NULL;

-- ── 4. Strip access_tokens to the key-grant matrix ──
-- Admin tokens (user_id NULL) and no-key login rows (credential_id NULL) are pure login
-- credentials — now in login_tokens only, so drop them here.
DELETE FROM access_tokens WHERE user_id IS NULL OR credential_id IS NULL;
ALTER TABLE access_tokens DROP COLUMN IF EXISTS name;
ALTER TABLE access_tokens DROP COLUMN IF EXISTS token_hash;
ALTER TABLE access_tokens DROP COLUMN IF EXISTS role;
ALTER TABLE access_tokens DROP COLUMN IF EXISTS role_id;
ALTER TABLE access_tokens DROP COLUMN IF EXISTS expires_at;
ALTER TABLE access_tokens DROP COLUMN IF EXISTS last_used_at;

-- ── 5. Indexes ──
-- The 0006 (user, credential) unique index now guards the grant matrix (keep it).
-- The 0007 "no channel pinned" unique index was login-only → move to login_tokens.
CREATE UNIQUE INDEX IF NOT EXISTS login_tokens_user_credential_uniq
    ON login_tokens (user_id, credential_id)
    WHERE user_id IS NOT NULL AND credential_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS login_tokens_user_no_cred_uniq
    ON login_tokens (user_id)
    WHERE user_id IS NOT NULL AND credential_id IS NULL;
DROP INDEX IF EXISTS access_tokens_user_no_cred_uniq;
