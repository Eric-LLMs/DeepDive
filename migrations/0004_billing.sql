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
    name                   TEXT NOT NULL UNIQUE,      -- virtual model name (referenced by roles)
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
    actual_model_name       TEXT NOT NULL,             -- provider's model id for this credential
    priority                INT NOT NULL DEFAULT 0,    -- lower = preferred
    weight                  INT NOT NULL DEFAULT 1,    -- load-balance weight
    prompt_price_per_1k     NUMERIC(12,6),             -- NULL = inherit llm_models
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
