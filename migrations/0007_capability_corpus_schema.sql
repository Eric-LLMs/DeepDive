-- Delveta schema migration 0007 — the Registry becomes the CANONICAL source of
-- the recall corpus and the parameter schema (2026-09-24 chain ruling).
--
-- Recall corpus: one capability's embedding material is
--     standard_example (one canonical sentence)
--   + synonym_examples (list of synonymous phrasings)
--   + examples         (pre-existing candidate expressions; kept as the third
--                       tier so the three seeded drafts stay valid)
-- all embedded per-SENTENCE (index granularity = example, many examples map to
-- one capability_id — already true of the qir example-vector index).
--
-- Parameter schema: ``parameters`` is the canonical per-capability argument
-- schema the Model A Candidate Card is assembled from:
--     {name: {"type", "description", "required", "max_len"?}}
-- DIRECT_TOOLS stays a Tool Binding / runtime compat layer only; the publish
-- gate mechanically cross-checks the two, so no human sync burden exists.
--
-- Idempotent like 0002-0006.

ALTER TABLE public.capabilities
    ADD COLUMN IF NOT EXISTS standard_example text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS synonym_examples jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS parameters     jsonb NOT NULL DEFAULT '{}'::jsonb;
