-- Delveta schema migration 0010 — funnel trace capture (Phase 6, minimal loop).
--
-- Optional per-event trace blob: when settings.chat_funnel_trace_capture is
-- ON, _persist_event attaches a rebuilt candidate-card summary + the query +
-- the model verdict (NEVER the full prompt — the verbatim prompt stays in the
-- log-based audit tooling). Default OFF: existing rows keep trace_json NULL
-- and the write path is otherwise byte-identical.
--
-- Idempotent re-runs are safe (IF NOT EXISTS), same discipline as 0006-0009.

ALTER TABLE public.chat_funnel_events
    ADD COLUMN IF NOT EXISTS trace_json jsonb;
