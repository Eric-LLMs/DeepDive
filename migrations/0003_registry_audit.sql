-- Delveta schema migration 0003 — Registry audit trail (QIR P1, step 4).
--
-- Minimal necessary audit storage (audit_logs was designed but never created).
-- Publish/Rollback MUST be audited, and a REJECTED publish produces no
-- registry_versions row, so this table is the only place the rejection (with
-- its issues) survives. Denormalized actor, no FKs (workspace_activity
-- precedent: entries must outlive the rows they reference).

CREATE TABLE IF NOT EXISTS public.registry_audit (
    id             uuid DEFAULT gen_random_uuid() NOT NULL,
    action         text NOT NULL,
    actor_username text,
    target         text,
    ok             boolean NOT NULL DEFAULT true,
    detail         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT registry_audit_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_registry_audit_created_at
    ON public.registry_audit (created_at);
