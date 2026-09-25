-- Delveta schema migration 0009 — the SQL Intent Query Library (Phase 3,
-- Action-Contract ruling 2026-09-25).
--
-- The recall corpus moves from the ``app_settings`` JSONB blob into real rows:
-- one row per SENTENCE per capability per published version, with its own
-- pgvector embedding. The corpus source is the curated intent set
-- (standard_example + synonym_examples via ``CapabilityEntry.intent_corpus``)
-- — legacy ``examples`` deliberately have NO path into this table.
--
-- Version discipline: every ANN query carries the single-version predicate
-- ``qir_version = <active>`` (read once per turn) — cross-version recall is
-- physically impossible here. The active version pointer itself stays in
-- ``app_settings`` (qir_version / qir_active): Build-Then-Swap writes rows and
-- swaps the pointer inside the SAME short transaction, so an observable state
-- of "active version has no rows" is a fault to report, not a half-publish.
--
-- Fresh installs run 0001 first (vector type exists via pgvector), then this
-- CREATE; idempotent re-runs are safe (IF NOT EXISTS), same discipline as
-- 0002-0008. HNSW cosine ops mirror the query's ``embedding <=> $1`` operator.

CREATE TABLE IF NOT EXISTS public.qir_examples (
    capability_id text NOT NULL,
    example_index integer NOT NULL,
    -- 'canonical' = position 0 (standard_example), 'synonym' = the rest.
    kind          text NOT NULL CONSTRAINT qir_examples_kind_check
                      CHECK (kind IN ('canonical', 'synonym')),
    -- CJK detection at build time — observability only, never a query filter.
    language      text NOT NULL CONSTRAINT qir_examples_language_check
                      CHECK (language IN ('zh', 'en')),
    text          text NOT NULL,
    embedding     public.vector(1024) NOT NULL,
    enabled       boolean NOT NULL DEFAULT true,
    -- the qir1-<fingerprint> of the snapshot these rows were built for
    qir_version   text NOT NULL,
    created_at    timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT qir_examples_pkey
        PRIMARY KEY (qir_version, capability_id, example_index)
);

CREATE INDEX IF NOT EXISTS qir_examples_embedding_hnsw
    ON public.qir_examples USING hnsw (embedding public.vector_cosine_ops);

-- The ANN query's driving predicate: one version, enabled rows only.
CREATE INDEX IF NOT EXISTS qir_examples_version_enabled_idx
    ON public.qir_examples (qir_version, enabled);
