-- Delveta schema migration 0004 — execution_mode on the usage ledger (QIR P1, step 5).
--
-- The 8.14 hole: ``user_usage_logs`` had no "what was this call FOR" column, so
-- every call — real user, shadow observer, admin preview build, test harness —
-- debited the same user wallet. One column closes it: the billing path
-- (``_log_usage``) settles non-production rows WITHOUT deducting, and the
-- user-facing usage report filters to ``production`` only. Real-user accounting
-- is untouched (existing rows are backfilled to the default).
--
-- Idempotent for the same reason as 0002/0003: fresh installs run 0001 first
-- (no such column yet) then this ALTER; partially-migrated DBs re-run safely.

ALTER TABLE public.user_usage_logs
    ADD COLUMN IF NOT EXISTS execution_mode text NOT NULL DEFAULT 'production';

-- Only the non-production minority is ever selected BY mode (telemetry grouping);
-- production reads keep riding the existing user_id/created_at indexes, so the
-- index covers the filtered rows only.
CREATE INDEX IF NOT EXISTS ix_user_usage_logs_execution_mode
    ON public.user_usage_logs (execution_mode, created_at)
    WHERE (execution_mode <> 'production');
