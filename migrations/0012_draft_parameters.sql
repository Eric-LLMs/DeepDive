-- Delveta schema migration 0012 — pre-Registry parameter configuration.
--
-- The Action-Universe Advanced tab must let an admin VIEW and CONFIGURE an
-- action's Tool parameters BEFORE it enters the Registry (Action-Universe
-- follow-up ruling 2026-09-25: "Agent-only != no parameters"). Two JSONB
-- columns hang off the existing Draft Configuration head:
--
--   parameters — same shape as capabilities.parameters (slot -> {type,
--                description, required, ...}); register copies it 1:1.
--   arg_slots  — same shape as capabilities.arg_slots (Binder mapping).
--
-- These are DRAFT configuration only, exactly like the query sentences from
-- 0011: nothing at runtime reads them; the only path to active stays
-- register -> validate -> publish. The tool schema itself is never stored
-- here — it is read live from ToolRuntime (GET /admin/registry/tool-schemas),
-- so this table can never become a second parameter truth.
-- Idempotent like 0002-0011.

ALTER TABLE public.action_query_drafts
    ADD COLUMN IF NOT EXISTS parameters JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.action_query_drafts
    ADD COLUMN IF NOT EXISTS arg_slots JSONB NOT NULL DEFAULT '{}'::jsonb;
