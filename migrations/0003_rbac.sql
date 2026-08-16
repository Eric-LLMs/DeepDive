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
