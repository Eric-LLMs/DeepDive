-- Delveta schema migration 0006 — Intent Funnel routing events (QIR P4, 8.12).
--
-- One row per funnel route decision — production turns and 8.5 query previews
-- alike — tagged by execution_mode so shadow/preview/test telemetry never
-- reads as user traffic (8.14). Telemetry-only: the request path never reads
-- this table back, and the write is best-effort (a DB fault must not sink a
-- turn). No FKs on user/session — events survive deletes — and the raw query
-- is deliberately NOT stored (8.12 privacy line).
--
-- Fresh installs run 0001 first (no such table), then this CREATE; idempotent
-- re-runs are safe (IF NOT EXISTS), same discipline as 0002-0005.

CREATE TABLE IF NOT EXISTS public.chat_funnel_events (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    execution_mode text NOT NULL DEFAULT 'production',
    user_id uuid,
    session_id text,
    deepest_stage text NOT NULL DEFAULT 'registry',
    matcher text,
    recall_count integer NOT NULL DEFAULT 0,
    recall_top text,
    judge text,
    decision text,
    final_route text NOT NULL DEFAULT 'agent',
    fallback_reason text,
    registry_version text,
    index_version text,
    capability_id text,
    total_ms integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chat_funnel_events_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_chat_funnel_events_created_at
    ON public.chat_funnel_events (created_at);
CREATE INDEX IF NOT EXISTS ix_chat_funnel_events_execution_mode
    ON public.chat_funnel_events (execution_mode);
