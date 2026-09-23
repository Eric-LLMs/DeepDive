-- Delveta schema migration 0005 — intent kind on the Registry draft table (P3).
--
-- P3 widens the CANDIDATE SPACE beyond plain ACTION (private/web capabilities
-- may now be authored in the table) WITHOUT widening routing: each kind carries
-- its own rollout gate in settings (chat_funnel_private_enabled /
-- chat_funnel_web_enabled, both default OFF), so "入表" and "开闸" stay two
-- independent, auditable steps (灰度令). Published payloads embed the kind;
-- pre-P3 versions have none and the read path defaults them to 'action' — the
-- historical kind — so an old active version can never route a widened one.
--
-- Idempotent for the same reason as 0002/0003/0004: fresh installs run 0001
-- first (no such column yet) then this ALTER; partially-migrated DBs re-run
-- safely.

ALTER TABLE public.capabilities
    ADD COLUMN IF NOT EXISTS intent_kind text NOT NULL DEFAULT 'action';

-- The enum lives in code (registry.types.VALID_KINDS, enforced by the publish
-- gate 8.4); this CHECK is the DB-side backstop against hand-edited rows only.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'capabilities_intent_kind_check'
    ) THEN
        ALTER TABLE public.capabilities
            ADD CONSTRAINT capabilities_intent_kind_check
            CHECK (intent_kind IN ('action', 'private', 'web'));
    END IF;
END $$;
