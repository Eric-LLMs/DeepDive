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
