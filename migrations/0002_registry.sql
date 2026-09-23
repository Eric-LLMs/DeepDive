-- Delveta schema migration 0002 — Intent Registry (QIR P1, step 1).
--
-- Two new tables land the Registry as a real, versioned store (docs/temp.md §8.2):
--   * capabilities        — the editable Draft rows (the single source of truth for
--                           routable capabilities). Runtime never reads this table.
--   * registry_versions   — immutable published versions; each publish/rollback stages
--                           a NEW version row and atomically swaps the single ``active``
--                           pointer. History is never updated in place.
--
-- Kept idempotent (IF NOT EXISTS) so it is safe against a partially-migrated DB and
-- re-runs. Fresh installs and already-migrated DBs both pick this up via the glob
-- loop in ``core.infrastructure.db.init_db`` (0001_init already recorded -> skipped,
-- 0002 applies).
--
-- gen_random_uuid() is available (pgcrypto is pulled in by the earlier tables' use of
-- it; see 0001_init.sql).

-- ── capabilities ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.capabilities (
    id              uuid DEFAULT gen_random_uuid() NOT NULL,
    capability_id   text NOT NULL,
    tool_binding    text NOT NULL,
    description     text NOT NULL DEFAULT '',
    patterns        jsonb NOT NULL DEFAULT '[]'::jsonb,
    aliases         jsonb NOT NULL DEFAULT '[]'::jsonb,
    examples        jsonb NOT NULL DEFAULT '[]'::jsonb,
    negatives       jsonb NOT NULL DEFAULT '[]'::jsonb,
    arg_slots       jsonb NOT NULL DEFAULT '{}'::jsonb,
    permissions     text NOT NULL DEFAULT '',
    execution_policy text NOT NULL DEFAULT 'auto',
    enabled         boolean NOT NULL DEFAULT true,
    status          text NOT NULL DEFAULT 'active',
    replacement_capability_id text,
    row_version     integer NOT NULL DEFAULT 0,
    created_at      timestamp with time zone NOT NULL DEFAULT now(),
    updated_at      timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT capabilities_pkey PRIMARY KEY (id),
    CONSTRAINT capabilities_capability_id_key UNIQUE (capability_id)
);

-- ── registry_versions ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.registry_versions (
    version        bigint NOT NULL,
    state          text NOT NULL DEFAULT 'staged',
    payload        jsonb NOT NULL,
    fingerprint    text NOT NULL,
    source_version bigint,
    actor_user_id  uuid,
    actor_username text,
    note           text,
    error          text,
    created_at     timestamp with time zone NOT NULL DEFAULT now(),
    activated_at   timestamp with time zone,
    CONSTRAINT registry_versions_pkey PRIMARY KEY (version)
);

CREATE INDEX IF NOT EXISTS ix_registry_versions_state
    ON public.registry_versions (state);

-- The atomic-swap invariant, pushed down into the DB: at most ONE row may be
-- ``active`` at any time. A partial unique index rejects a second active insert
-- even across racing transactions, so Build-Then-Swap can never leave two actives.
CREATE UNIQUE INDEX IF NOT EXISTS uq_registry_versions_single_active
    ON public.registry_versions (state)
    WHERE (state = 'active');
