-- 0001_init.sql: DeepDive full schema (consolidated).
--
-- Single shipped script for end users: the squash of the original 0001_init.sql through
-- 0008_login_tokens.sql applied in order (a fresh install produces the exact same schema
-- as running all eight). Versioned per-change migrations are a development concern; the
-- release schema is always this one consolidated file.
--
-- Idempotent: statements are IF NOT EXISTS / guarded, so re-running is safe.


-- 0001_init: initial schema.
--
-- Extensions first, then core tables. Idempotent (IF NOT EXISTS) so it is safe to re-run
-- against a database that was already created by the previous Alembic migration.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS domains (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS materials (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type        TEXT NOT NULL,               -- 'domain' | 'video' | 'document'
    title       TEXT NOT NULL,
    source_url  TEXT,
    meta        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS terms (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id   UUID NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    word        TEXT NOT NULL,
    definition  TEXT,
    frequency   INTEGER NOT NULL,
    star_level  INTEGER NOT NULL,
    audio_hash  TEXT,
    image_paths JSONB NOT NULL,
    is_active   BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS sentences (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id      UUID NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    origin_source  TEXT,
    content_en     TEXT NOT NULL UNIQUE,
    content_cn     TEXT,
    audio_hash     TEXT,
    cn_explanation TEXT,
    embedding      vector(1024)
);

CREATE TABLE IF NOT EXISTS chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    material_id UUID NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    content_en  TEXT NOT NULL,
    content_cn  TEXT,
    meta        JSONB NOT NULL,
    embedding   vector(1024) NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ,
    summary     TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,               -- 'user' | 'assistant' | 'tool'
    text        TEXT NOT NULL,
    embedding   vector(1024),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS session_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    type        TEXT NOT NULL,
    timestamp   DOUBLE PRECISION NOT NULL,
    payload     JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    term_id        UUID NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    sentence_id    UUID NOT NULL REFERENCES sentences(id) ON DELETE CASCADE,
    cn_explanation TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type         TEXT NOT NULL,
    status       TEXT NOT NULL,
    payload      JSONB NOT NULL,
    result       JSONB,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);


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


-- 0003_rbac: role-based access control + opaque API tokens + usage accounting.
--
-- Replaces the flat ``users.tier`` column with a proper roles table, adds opaque access
-- tokens (server-side, revocable, multi-token), and per-user usage counters plus an
-- append-only usage log for audit. Idempotent (IF NOT EXISTS / DO NOTHING / guarded DDL).

-- ── 1. Roles (quota + model + feature permissions live on the role) ──
CREATE TABLE IF NOT EXISTS user_roles (
    role_id               TEXT PRIMARY KEY,
    role_name             TEXT NOT NULL,
    daily_request_limit   INT NOT NULL DEFAULT 50,      -- -1 = unlimited
    monthly_request_limit INT NOT NULL DEFAULT 1500,    -- -1 = unlimited
    daily_token_limit     BIGINT DEFAULT -1,            -- -1 = unlimited
    rpm_limit             INT DEFAULT -1,               -- -1 = unlimited
    monthly_cost_limit    NUMERIC(12,6) DEFAULT -1,     -- -1 = unlimited
    default_model         TEXT DEFAULT '',              -- empty = use active provider model
    models                TEXT[] DEFAULT '{}',          -- allowed model ids (empty = all)
    features              JSONB NOT NULL DEFAULT '{}'::jsonb,  -- e.g. {"chat": true}
    is_active             BOOLEAN NOT NULL DEFAULT true,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO user_roles
    (role_id, role_name, daily_request_limit, monthly_request_limit, daily_token_limit,
     rpm_limit, monthly_cost_limit, default_model, models, features)
VALUES
    ('regular', '普通用户', 50,  1500,  -1, -1, -1, '', '{}', '{"chat": true}'::jsonb),
    ('pro',     '专业版',   500, 15000, -1, -1, -1, '', '{}', '{"chat": true}'::jsonb),
    ('vip',     'VIP',      -1,  -1,    -1, -1, -1, '', '{}', '{"chat": true}'::jsonb),
    ('admin',   '管理员',    -1,  -1,    -1, -1, -1, '', '{}', '{"chat": true}'::jsonb)
ON CONFLICT (role_id) DO NOTHING;

-- ── 2. users: flat tier → role_id (backfill existing rows) ──
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS role_id    TEXT,
    ADD COLUMN IF NOT EXISTS meta       JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

UPDATE users SET role_id = tier WHERE role_id IS NULL AND tier IN ('regular', 'pro', 'vip', 'admin');
UPDATE users SET role_id = 'regular' WHERE role_id IS NULL;

ALTER TABLE users ALTER COLUMN role_id SET DEFAULT 'regular';
ALTER TABLE users ALTER COLUMN role_id SET NOT NULL;

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_id_fkey;
ALTER TABLE users ADD CONSTRAINT users_role_id_fkey
    FOREIGN KEY (role_id) REFERENCES user_roles (role_id) ON DELETE RESTRICT;

ALTER TABLE users DROP COLUMN IF EXISTS tier;

-- ── 3. Opaque access tokens (login tokens + admin-minted API tokens) ──
CREATE TABLE IF NOT EXISTS access_tokens (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID REFERENCES users (id) ON DELETE CASCADE,   -- NULL = admin token
    name         TEXT NOT NULL,                                  -- human label
    token_hash   TEXT NOT NULL UNIQUE,                           -- sha256(raw token)
    role         TEXT NOT NULL DEFAULT 'user',                   -- 'admin' | 'user'
    role_id      TEXT REFERENCES user_roles (role_id) ON DELETE SET NULL,  -- optional role override
    expires_at   TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    is_active    BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS access_tokens_user_id_idx ON access_tokens (user_id);

-- ── 4. Usage counters (O(1) atomic quota enforcement; NOT redis, NOT COUNT over logs) ──
CREATE TABLE IF NOT EXISTS user_usage_counters (
    user_id       UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    period_type   TEXT NOT NULL,          -- 'day' | 'month'
    period_start  DATE NOT NULL,          -- today's date, or first-of-month
    request_count BIGINT NOT NULL DEFAULT 0,
    token_count   BIGINT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, period_type, period_start)
);

-- ── 5. Usage log (append-only audit) ──
CREATE TABLE IF NOT EXISTS user_usage_logs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID REFERENCES users (id) ON DELETE SET NULL,
    token_id          UUID REFERENCES access_tokens (id) ON DELETE SET NULL,
    role_id           TEXT,               -- role snapshot at call time
    model_name        TEXT,
    tool              TEXT,
    prompt_tokens     INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    total_tokens      INT NOT NULL DEFAULT 0,   -- prompt + completion; denormalized for dashboards
    cost_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_usage_logs_user_id_idx ON user_usage_logs (user_id, created_at);
CREATE INDEX IF NOT EXISTS user_usage_logs_token_id_idx ON user_usage_logs (token_id, created_at);


-- 0004_billing: provider normalization + per-model pricing + wallet ledger.
--
-- Complements the RBAC module (0003) with the billing side:
--   * llm_credentials / llm_models / credential_models  — provider key + model catalog + N:M routing
--   * user_wallets / wallet_transactions                — cash wallet + append-only ledger (balance_after)
--
-- Pricing on llm_models is the cost source for PAYG billing (prompt/completion price per 1k tokens);
-- credential_models may override it per credential. Idempotent.

-- ── 1. Provider API credentials (manually maintained) ──
CREATE TABLE IF NOT EXISTS llm_credentials (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    base_url   TEXT NOT NULL,
    api_key    TEXT NOT NULL,
    is_active  BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ
);

-- ── 2. Model catalog + pricing ──
CREATE TABLE IF NOT EXISTS llm_models (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                   TEXT NOT NULL UNIQUE,      -- display name (referenced by roles)
    provider_model_name    TEXT,                      -- real model id on the provider platform
    description            TEXT,
    prompt_price_per_1k    NUMERIC(12,6) NOT NULL DEFAULT 0,
    completion_price_per_1k NUMERIC(12,6) NOT NULL DEFAULT 0,
    is_active              BOOLEAN NOT NULL DEFAULT true,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 3. Credential ↔ model routing (N:M; failover priority + load weight + per-key override) ──
CREATE TABLE IF NOT EXISTS credential_models (
    credential_id           UUID NOT NULL REFERENCES llm_credentials (id) ON DELETE CASCADE,
    model_id                UUID NOT NULL REFERENCES llm_models (id) ON DELETE CASCADE,
    note                    TEXT,                       -- free-text route purpose ("what this is for")
    priority                INT NOT NULL DEFAULT 0,     -- lower = preferred
    weight                  INT NOT NULL DEFAULT 1,     -- load-balance weight
    prompt_price_per_1k     NUMERIC(12,6),              -- NULL = inherit llm_models
    completion_price_per_1k NUMERIC(12,6),
    is_active               BOOLEAN NOT NULL DEFAULT true,
    PRIMARY KEY (credential_id, model_id)
);

-- ── 4. Cash wallets (one row per user) ──
CREATE TABLE IF NOT EXISTS user_wallets (
    user_id    UUID PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    balance    NUMERIC(14,6) NOT NULL DEFAULT 0,
    currency   TEXT NOT NULL DEFAULT 'USD',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 5. Wallet ledger (append-only; balance_after is a snapshot, never recomputed) ──
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    type            TEXT NOT NULL,                     -- 'topup' | 'llm_consume' | 'refund' | 'adjustment'
    amount          NUMERIC(14,6) NOT NULL,            -- +credit / -debit
    balance_after   NUMERIC(14,6) NOT NULL,
    description     TEXT,
    meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key TEXT UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wallet_transactions_user_idx ON wallet_transactions (user_id, created_at);


-- 0005_roles_credentials: role ↔ channel binding + credential pinning on tokens + anonymous role.
--
-- Complements 0003 (RBAC) and 0004 (billing):
--   * role_credentials          — N:M "which LLM channels (llm_credentials) a role may use"
--   * access_tokens.credential_id — the channel randomly picked from the role at login and pinned
--   * anonymous role            — guest tier with its own channel set and limits
--
-- One idempotent unit (final shape in a single pass, no repeated ALTERs). Applied in filename order.

-- ── 1. Role ↔ channel binding (N:M) ──
CREATE TABLE IF NOT EXISTS role_credentials (
    role_id       TEXT    NOT NULL REFERENCES user_roles (role_id)  ON DELETE CASCADE,
    credential_id UUID    NOT NULL REFERENCES llm_credentials (id) ON DELETE CASCADE,
    is_active     BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (role_id, credential_id)
);
CREATE INDEX IF NOT EXISTS role_credentials_credential_idx ON role_credentials (credential_id);

-- ── 2. Token credential pinning (channel chosen at login) ──
ALTER TABLE access_tokens ADD COLUMN IF NOT EXISTS credential_id UUID
    REFERENCES llm_credentials (id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS access_tokens_credential_id_idx ON access_tokens (credential_id);

-- ── 3. Anonymous guest role (seeded; guests route through its channels) ──
INSERT INTO user_roles
    (role_id, role_name, daily_request_limit, monthly_request_limit, daily_token_limit,
     rpm_limit, monthly_cost_limit, default_model, models, features)
VALUES
    ('anonymous', '匿名用户', 20, 600, -1, -1, -1, '', '{}', '{"chat": true}'::jsonb)
ON CONFLICT (role_id) DO NOTHING;


-- 0006_unique_user_tokens: one row per (user, pinned channel); re-login updates in place.
--
-- Before this, every login minted a fresh access_tokens row, so heavy logins grew the
-- table unboundedly. Now user tokens are reused per (user, credential_id):
--   * dedupe historical rows (keep the newest per pair) before adding the guard
--   * a partial unique index on (user_id, credential_id) where both are non-null
--     prevents duplicates; admin tokens (user_id NULL) and manual user tokens
--     (credential_id NULL) stay exempt, and the login endpoint reuses those rows
--     in application code instead.
--
-- One idempotent unit (final shape in a single pass).

-- ── 1. Dedupe existing login tokens: keep the newest row per (user, channel) ──
DELETE FROM access_tokens a
USING access_tokens b
WHERE a.user_id IS NOT NULL
  AND a.user_id = b.user_id
  AND a.credential_id IS NOT DISTINCT FROM b.credential_id
  AND (a.created_at < b.created_at
       OR (a.created_at = b.created_at AND a.id < b.id));

-- ── 2. Guard: at most one row per (user, pinned channel) ──
CREATE UNIQUE INDEX IF NOT EXISTS access_tokens_user_credential_uniq
    ON access_tokens (user_id, credential_id)
    WHERE user_id IS NOT NULL AND credential_id IS NOT NULL;


-- 0007_console_tokens_stateless: admin console logins no longer persist a token row.
--
-- The console now returns a signed, stateless session token (HMAC, held in the browser)
-- instead of minting an access_tokens row, so every browser login no longer grows the
-- table. This migration removes the leftover console-login rows (role='admin' with no
-- user and no channel, named after the console account) and hardens the
-- (user, channel) uniqueness guarantee for the "no channel pinned" case, which the
-- 0006 partial index intentionally did not cover.

-- ── 1. Drop leftover console-login rows ──
-- Console logins minted rows with role='admin', no user, no channel, and the console
-- username as the token name. Tokens-page admin API tokens (user-chosen names) are kept.
DELETE FROM access_tokens
WHERE role = 'admin'
  AND user_id IS NULL
  AND credential_id IS NULL
  AND name = (SELECT COALESCE(value->>'username', 'admin') FROM app_settings WHERE key = 'admin');

-- ── 2. Dedupe legacy (user, no-channel) rows: keep the newest per user ──
DELETE FROM access_tokens a
USING access_tokens b
WHERE a.user_id IS NOT NULL
  AND a.credential_id IS NULL
  AND a.user_id = b.user_id
  AND b.credential_id IS NULL
  AND (a.created_at < b.created_at
       OR (a.created_at = b.created_at AND a.id < b.id));

-- ── 3. Guard: at most one (user, no-channel) token per user ──
-- Complements 0006's index (which only covers pinned channels) so the uniqueness rule
-- holds for both cases: one row per (user, channel) whether the channel is pinned or not.
CREATE UNIQUE INDEX IF NOT EXISTS access_tokens_user_no_cred_uniq
    ON access_tokens (user_id)
    WHERE user_id IS NOT NULL AND credential_id IS NULL;


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


