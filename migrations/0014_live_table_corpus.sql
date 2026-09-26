-- Delveta schema migration 0014 — Live-Table Query Corpus (final ruling 2026-09-26).
--
-- A1 Live-Table ruling: capabilities + capability_standard_queries +
-- capability_similar_queries + capability_negatives ARE the runtime truth.
-- The Draft -> Publish -> Runtime-Projection lane (qir_examples, the JSONB
-- recall columns, the write_active pointer) is retired by this migration.
--
-- Data contract (A2, hard gate): the 550 curated sentences move COMPLETELY —
--   18 zh Standard (draft_items kind='standard' / capabilities.standard_example)
-- + 18 en Standard (the FIRST similar/synonym of every action, PROMOTED not copied)
-- = 36 rows in capability_standard_queries
--   514 remaining similar/synonym rows -> capability_similar_queries
-- (standard_query_id -> same (capability, language) Standard; zh=266 / en=248)
-- Total preserved: 36 + 514 = 550. Nothing is generated, deleted, rewritten or
-- re-classified here: INVALID/BOUNDARY dispositions happen AFTER migration.
--
-- language is MATERIALIZED here from the frozen derivation rule
-- (contains a Han char U+4E00-U+9FFF -> 'zh', else 'en'); no languages table.
--
-- Embeddings are NOT touched by this migration (no external service inside
-- migration SQL): the columns start NULL and scripts/embed_corpus.py backfills
-- them out-of-band; recall predicates filter embedding IS NOT NULL so the
-- chain honestly reports RECALL_UNAVAILABLE until the backfill has run.
--
-- Every validation below runs INSIDE the migration transaction (init_db wraps
-- each file in one): any mismatch aborts the whole thing — the old tables keep
-- serving, nothing half-happens.

-- ── 1. runtime truth tables ──────────────────────────────────────────────────────

CREATE TABLE capability_standard_queries (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_id   text NOT NULL REFERENCES capabilities(capability_id),
    query           text NOT NULL,
    language        text NOT NULL CHECK (language IN ('zh', 'en')),
    embedding       public.vector(1024),           -- backfilled out-of-band
    enabled         boolean NOT NULL DEFAULT true,
    position        integer NOT NULL DEFAULT 0,
    created_at      timestamp with time zone NOT NULL DEFAULT now(),
    updated_at      timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT capability_standard_queries_one_per_language
        UNIQUE (capability_id, language)
);

CREATE TABLE capability_similar_queries (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- A3: Similar belongs to a Capability ONLY through its Standard Query —
    -- no capability_id column (a second, drift-prone relation is forbidden).
    standard_query_id  uuid NOT NULL REFERENCES capability_standard_queries(id),
    query              text NOT NULL,
    language           text NOT NULL CHECK (language IN ('zh', 'en')),
    embedding          public.vector(1024),        -- backfilled out-of-band
    enabled            boolean NOT NULL DEFAULT true,
    position           integer NOT NULL DEFAULT 0,
    created_at         timestamp with time zone NOT NULL DEFAULT now(),
    updated_at         timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE capability_negatives (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_id   text NOT NULL REFERENCES capabilities(capability_id),
    query           text NOT NULL,
    language        text NOT NULL CHECK (language IN ('zh', 'en')),
    enabled         boolean NOT NULL DEFAULT true,
    position        integer NOT NULL DEFAULT 0,
    created_at      timestamp with time zone NOT NULL DEFAULT now(),
    updated_at      timestamp with time zone NOT NULL DEFAULT now()
    -- deliberately NO embedding column: negatives never participate in Recall
    -- (card boundary context only).
);

CREATE INDEX capability_standard_queries_embedding_hnsw
    ON capability_standard_queries USING hnsw (embedding public.vector_cosine_ops);
CREATE INDEX capability_similar_queries_embedding_hnsw
    ON capability_similar_queries USING hnsw (embedding public.vector_cosine_ops);
CREATE INDEX capability_similar_queries_standard_idx
    ON capability_similar_queries (standard_query_id);

-- ── 2. the 18 Capabilities (cap-<action_key> for the 15 not yet registered) ──────
-- parameters/arg_slots are copied from the pre-Registry Draft Configuration
-- FIRST — only after this INSERT can action_query_drafts ever be dropped.

INSERT INTO capabilities (
    capability_id, tool_binding, description,
    patterns, aliases, examples,
    parameters, arg_slots,
    permissions, execution_policy, intent_kind,
    enabled, status, row_version
)
SELECT
    'cap-' || replace(ac.action_key, '_', '-'),
    ac.tool_binding,
    ac.description,
    '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
    coalesce(d.parameters, '{}'::jsonb),
    coalesce(d.arg_slots,   '{}'::jsonb),
    '', 'auto', 'action',
    true, 'active', 0
FROM action_catalog ac
LEFT JOIN action_query_drafts d ON d.action_key = ac.action_key
WHERE NOT EXISTS (
    SELECT 1 FROM capabilities c WHERE c.tool_binding = ac.tool_binding
);

-- Safety: every catalog action must now have exactly one capability.
DO $$
DECLARE n int;
BEGIN
    SELECT count(*) INTO n FROM capabilities c
      JOIN action_catalog ac ON ac.tool_binding = c.tool_binding;
    IF n <> 18 THEN
        RAISE EXCEPTION '0014: expected 18 capability rows joined to the catalog, got %', n;
    END IF;
    SELECT count(*) INTO n FROM capabilities c
      JOIN action_catalog ac ON ac.tool_binding = c.tool_binding
     WHERE c.parameters IS NULL OR jsonb_typeof(c.parameters) <> 'object'
        OR c.arg_slots   IS NULL OR jsonb_typeof(c.arg_slots)   <> 'object';
    IF n <> 0 THEN
        RAISE EXCEPTION '0014: % capabilities lack migrated parameters/arg_slots', n;
    END IF;
END $$;

-- ── 3. move the 550 sentences ────────────────────────────────────────────────────
-- Source normalization: every sentence becomes (capability_id, query, language,
-- kind, position) where kind='standard' rows are the 18 zh standards and the 18
-- FIRST-similar/synonym rows promoted to English Standard.

WITH cap AS (
    SELECT c.capability_id, ac.action_key
      FROM capabilities c
      JOIN action_catalog ac ON ac.tool_binding = c.tool_binding
),
-- draft lane: 15 actions in action_query_draft_items
draft_src AS (
    SELECT cap.capability_id,
           i.text                                   AS query,
           i.kind                                   AS src_kind,
           i.position,
           i.enabled,
           i.created_at, i.updated_at,
           -- promotion rule: the first (position,id) 'similar' of each action IS
           -- its English Standard — zh-contains -> zh else en
           CASE WHEN i.text ~ '[一-鿿]' THEN 'zh' ELSE 'en' END AS language,
           row_number() OVER (PARTITION BY i.action_key, i.kind
                              ORDER BY i.position, i.id)         AS rn
      FROM action_query_draft_items i
      JOIN cap ON cap.action_key = i.action_key
),
draft_std AS (
    SELECT capability_id, query,
           CASE WHEN query ~ '[一-鿿]' THEN 'zh' ELSE 'en' END AS language,
           position, created_at, updated_at
      FROM draft_src WHERE src_kind = 'standard'
    UNION ALL
    SELECT capability_id, query, 'en'::text, position, created_at, updated_at
      FROM draft_src WHERE src_kind = 'similar' AND rn = 1
),
draft_sim AS (
    SELECT capability_id, query, language, position, enabled, created_at, updated_at
      FROM draft_src WHERE src_kind = 'similar' AND rn > 1
),
-- registry lane: the 3 caps living in the JSONB columns
reg_src AS (
    SELECT cap.capability_id,
           c.standard_example                       AS query,
           'standard'::text                         AS src_kind,
           0 AS position, true AS enabled,
           c.created_at, c.updated_at
      FROM capabilities c JOIN cap ON cap.capability_id = c.capability_id
     WHERE coalesce(c.standard_example, '') <> ''
    UNION ALL
    SELECT cap.capability_id,
           s.value, 'similar', (s.n - 1)::int, true, c.created_at, c.updated_at
      FROM capabilities c
      JOIN cap ON cap.capability_id = c.capability_id
      CROSS JOIN LATERAL jsonb_array_elements_text(c.synonym_examples)
           WITH ORDINALITY AS s(value, n)
     WHERE jsonb_typeof(c.synonym_examples) = 'array'
),
reg_std AS (
    SELECT capability_id, query,
           CASE WHEN query ~ '[一-鿿]' THEN 'zh' ELSE 'en' END AS language,
           position, created_at, updated_at
      FROM reg_src
     WHERE src_kind = 'standard'
        OR (src_kind = 'similar' AND position = 0)   -- the promoted EN anchor
),
reg_sim AS (
    SELECT capability_id, query,
           CASE WHEN query ~ '[一-鿿]' THEN 'zh' ELSE 'en' END AS language,
           position, enabled, created_at, updated_at
      FROM reg_src
     WHERE src_kind = 'similar' AND position > 0
),
all_std AS (
    SELECT * FROM draft_std UNION ALL SELECT * FROM reg_std
),
ins_std AS (
    INSERT INTO capability_standard_queries
           (capability_id, query, language, position, created_at, updated_at)
    SELECT capability_id, query, language, position, created_at, updated_at
      FROM all_std
    RETURNING id, capability_id, language
),
all_sim AS (
    SELECT * FROM draft_sim UNION ALL SELECT * FROM reg_sim
)
INSERT INTO capability_similar_queries
       (standard_query_id, query, language, position, enabled, created_at, updated_at)
SELECT st.id, x.query, x.language, x.position, x.enabled, x.created_at, x.updated_at
  FROM all_sim x
  JOIN ins_std st ON st.capability_id = x.capability_id
                 AND st.language      = x.language;

-- ── 4. the hard acceptance gate: per-capability before/after reconciliation ──────
-- Any mismatch aborts the transaction: the old tables keep serving untouched.

DO $$
DECLARE
    n_std   int; n_sim int; n_src int; n_bad int; n_add int; bad record;
BEGIN
    SELECT count(*) INTO n_std FROM capability_standard_queries;
    SELECT count(*) INTO n_sim FROM capability_similar_queries;
    IF n_std <> 36 THEN
        RAISE EXCEPTION '0014: Standard must be exactly 36, got %', n_std;
    END IF;
    IF n_sim <> 514 THEN
        RAISE EXCEPTION '0014: Similar must be exactly 514, got %', n_sim;
    END IF;
    IF n_std + n_sim <> 550 THEN
        RAISE EXCEPTION '0014: corpus total must be 550, got %', n_std + n_sim;
    END IF;

    -- every capability: exactly one zh + one en Standard
    FOR bad IN
        SELECT c.capability_id
          FROM capabilities c
          JOIN action_catalog ac ON ac.tool_binding = c.tool_binding
          LEFT JOIN capability_standard_queries s ON s.capability_id = c.capability_id
         GROUP BY c.capability_id
        HAVING count(*) FILTER (WHERE s.language = 'zh') <> 1
            OR count(*) FILTER (WHERE s.language = 'en') <> 1
    LOOP
        RAISE EXCEPTION '0014: % lacks exactly one zh+en Standard', bad.capability_id;
    END LOOP;

    -- Similar language must equal its Standard's language (FK guarantees
    -- existence; this catches any derivation drift).
    SELECT count(*) INTO n_bad FROM capability_similar_queries q
      JOIN capability_standard_queries s ON s.id = q.standard_query_id
     WHERE q.language <> s.language;
    IF n_bad <> 0 THEN
        RAISE EXCEPTION '0014: % similar rows disagree with their Standard language', n_bad;
    END IF;

    -- per-capability total reconciliation against the STILL-PRESENT sources
    -- (draft lane items + registry lane: 1 standard_example + synonym array)
    SELECT count(*) INTO n_src
      FROM action_query_draft_items
     WHERE kind IN ('standard', 'similar');
    SELECT n_src + coalesce(sum(
               (CASE WHEN coalesce(standard_example, '') <> '' THEN 1 ELSE 0 END)
               + (CASE WHEN jsonb_typeof(synonym_examples) = 'array'
                       THEN jsonb_array_length(synonym_examples) ELSE 0 END)), 0)
      INTO n_src
      FROM capabilities
     WHERE capability_id IN ('cap-add-term', 'cap-create-folder', 'cap-pdf-extract-text');
    IF n_std + n_sim <> n_src THEN
        RAISE EXCEPTION '0014: before/after total mismatch: sources=% migrated=%',
            n_src, n_std + n_sim;
    END IF;

    -- no sentence text may have been altered: multiset equality of the OLD
    -- source sentences and the NEW 36+514 set (both directions).
    SELECT count(*) INTO n_bad FROM (
        (SELECT text AS query FROM action_query_draft_items
          WHERE kind IN ('standard', 'similar')
         UNION ALL
         SELECT standard_example FROM capabilities
          WHERE capability_id IN ('cap-add-term','cap-create-folder','cap-pdf-extract-text')
            AND coalesce(standard_example, '') <> ''
         UNION ALL
         SELECT s FROM capabilities c
           CROSS JOIN LATERAL jsonb_array_elements_text(
               CASE WHEN jsonb_typeof(c.synonym_examples) = 'array'
                    THEN c.synonym_examples ELSE '[]'::jsonb END) AS t(s)
          WHERE c.capability_id IN ('cap-add-term','cap-create-folder','cap-pdf-extract-text'))
        EXCEPT ALL
        (SELECT query FROM capability_standard_queries
         UNION ALL
         SELECT query FROM capability_similar_queries)
    ) gone;
    SELECT count(*) INTO n_add FROM (
        (SELECT query FROM capability_standard_queries
         UNION ALL
         SELECT query FROM capability_similar_queries)
        EXCEPT ALL
        (SELECT text FROM action_query_draft_items
          WHERE kind IN ('standard', 'similar')
         UNION ALL
         SELECT standard_example FROM capabilities
          WHERE capability_id IN ('cap-add-term','cap-create-folder','cap-pdf-extract-text')
            AND coalesce(standard_example, '') <> ''
         UNION ALL
         SELECT s FROM capabilities c
           CROSS JOIN LATERAL jsonb_array_elements_text(
               CASE WHEN jsonb_typeof(c.synonym_examples) = 'array'
                    THEN c.synonym_examples ELSE '[]'::jsonb END) AS t(s)
          WHERE c.capability_id IN ('cap-add-term','cap-create-folder','cap-pdf-extract-text')))
        added;
    IF n_bad <> 0 OR n_add <> 0 THEN
        RAISE EXCEPTION '0014: sentence multiset changed during migration (gone=% added=%)',
            n_bad, n_add;
    END IF;
END $$;

-- ── 5. retire the old lane ───────────────────────────────────────────────────────

-- examples was always card-context, never a Recall anchor (ruling 2026-09-25):
-- it is RENAMED to its real meaning, content untouched.
ALTER TABLE capabilities RENAME COLUMN examples TO request_query_examples;

ALTER TABLE capabilities
    DROP COLUMN standard_example,
    DROP COLUMN synonym_examples,
    DROP COLUMN negatives;

DROP TABLE qir_examples;
DROP TABLE action_query_draft_items;
DROP TABLE action_query_drafts;

-- the old active-snapshot pointer rows (qir/store.py) are gone with the store.
DELETE FROM app_settings WHERE key IN ('qir_version', 'qir_active');

-- registry_versions keeps its rows as HISTORY ONLY (A1): the publish chain that
-- wrote it is deleted in code; nothing in the runtime reads this table any more.
