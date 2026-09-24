-- Delveta schema migration 0008 — responsibility naming for the selection+
-- extraction component (2026-09-24 naming ruling).
--
-- "Model A" was a placeholder and "Judge"/"Decision" were historical names;
-- the component that does capability/tool selection AND argument extraction
-- is the ToolIntentModel. Telemetry vocabulary follows the code:
--   * chat_funnel_events.judge    -> chat_funnel_events.tool_intent
--   * chat_funnel_events.decision -> dropped (the retired Decision node's
--     column was pinned "-" since the single-hop ruling; no name survives).
--
-- Idempotent like 0002-0007 (catalog-guarded, safe to re-run).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND table_name = 'chat_funnel_events'
                 AND column_name = 'judge') THEN
        ALTER TABLE public.chat_funnel_events RENAME COLUMN judge TO tool_intent;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND table_name = 'chat_funnel_events'
                 AND column_name = 'decision') THEN
        ALTER TABLE public.chat_funnel_events DROP COLUMN decision;
    END IF;
END $$;
