-- Delveta schema migration 0011 — Action Catalog (Action-Universe ruling 2026-09-25).
--
-- The Action Universe (every user-facing Action that exists in the system) is NOT
-- the Registry: three strictly separated layers, one direction of promotion —
--
--   action_catalog              — inventory of user-facing Actions (this migration,
--                                 seeded EXPLICITLY from the audited 2026-09-25
--                                 Action Universe code review — 18 rows; never
--                                 generated from synthetic workloads, queries, or
--                                 *_tool.py auto-discovery).
--   capabilities                — Actions that entered the Intent Registry (Draft).
--   registry_versions/qir_...   — published / active routing material (untouched).
--
-- Agent-only Actions keep their pre-Registry query configuration in:
--   action_query_drafts         — one Draft Configuration head per action_key.
--   action_query_draft_items    — kind = standard | similar | negative
--                                 (maps to standard_example / synonym_examples /
--                                 negatives when — and only when — an admin
--                                 explicitly registers the action as a Draft
--                                 capability; from there the ONLY path to active
--                                 is the existing Draft -> Validate -> Publish
--                                 lifecycle. Nothing in this schema can land a
--                                 sentence in qir_examples or the active index.)
--
-- No hard-delete discipline (8.6) applies here too: rows flip status, they are not
-- removed. Idempotent (IF NOT EXISTS / ON CONFLICT DO NOTHING) like 0002-0010, so
-- re-runs and partially-migrated DBs are safe.

CREATE TABLE IF NOT EXISTS public.action_catalog (
    action_key          text NOT NULL,          -- stable identity, e.g. 'summary'
    display_name        text NOT NULL,
    description         text NOT NULL DEFAULT '',
    tool_binding        text NOT NULL,          -- the tool the Agent path executes
    route               text NOT NULL DEFAULT 'agent',
    implementation_ref  text NOT NULL DEFAULT '',
    -- user_facing rows appear in the Universe; deprecated rows are kept for audit
    status              text NOT NULL DEFAULT 'user_facing'
        CONSTRAINT action_catalog_status_check CHECK (status IN ('user_facing', 'deprecated')),
    created_at  timestamp with time zone NOT NULL DEFAULT now(),
    updated_at  timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT action_catalog_pkey PRIMARY KEY (action_key)
);

-- One open Draft Configuration per catalog action (the items below hang off it).
CREATE TABLE IF NOT EXISTS public.action_query_drafts (
    action_key  text NOT NULL
        CONSTRAINT action_query_drafts_action_key_fkey
            REFERENCES public.action_catalog (action_key),
    updated_by  text,
    created_at  timestamp with time zone NOT NULL DEFAULT now(),
    updated_at  timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT action_query_drafts_pkey PRIMARY KEY (action_key)
);

CREATE TABLE IF NOT EXISTS public.action_query_draft_items (
    id          uuid DEFAULT gen_random_uuid() NOT NULL,
    action_key  text NOT NULL
        CONSTRAINT action_query_draft_items_action_key_fkey
            REFERENCES public.action_catalog (action_key),
    -- 'standard' -> standard_example, 'similar' -> synonym_examples,
    -- 'negative' -> negatives — same semantics, but DRAFT ONLY until publish.
    kind        text NOT NULL
        CONSTRAINT action_query_draft_items_kind_check
            CHECK (kind IN ('standard', 'similar', 'negative')),
    text        text NOT NULL,
    position    integer NOT NULL DEFAULT 0,
    enabled     boolean NOT NULL DEFAULT true,
    created_at  timestamp with time zone NOT NULL DEFAULT now(),
    updated_at  timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT action_query_draft_items_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS action_query_draft_items_key_idx
    ON public.action_query_draft_items (action_key, kind, position);

-- ── Seed: the audited Action Universe (code review 2026-09-25) ──────────────────
-- 18 user-facing Actions. Kernel meta tools (tool_search, plan, run_subagent,
-- revert_to_checkpoint, tool_audit, memory*) are deliberately NOT user-facing and
-- are not seeded. Downgraded text wrappers (doc_outline / doc_mindmap / doc_slides)
-- are superseded by the toolkit tools below and are not seeded either.
-- route='agent' records the implementation path; the ADMIN API derives the
-- live route by joining ``capabilities`` on tool_binding (single source, no drift).
INSERT INTO public.action_catalog
    (action_key, display_name, description, tool_binding, route, implementation_ref)
VALUES
    ('add_term',          'Add Term',            'Add a term to a named vocabulary domain.',                          'add_term',          'agent', 'apps/api/tools/add_term_tool.py'),
    ('create_folder',     'Create Folder',       'Create a folder with an explicit name.',                            'create_folder',     'agent', 'apps/api/tools/create_folder_tool.py'),
    ('pdf_extract_text',  'PDF Extract Text',    'Extract the full text of the attached document.',                   'pdf_extract_text',  'agent', 'apps/api/tools/pdf_tools.py'),
    ('summary',           'Summary',             'Grounded Markdown research summary with [file:line] citations.',     'summary_gen',       'agent', 'apps/api/tools/toolkit (pipeline)'),
    ('mindmap',           'Mindmap',             'Mermaid mind map (.mmd) from one or more workspace files.',          'mindmap_gen',       'agent', 'apps/api/tools/toolkit (pipeline)'),
    ('slides',            'Slides / PPT',        'Slide deck: canonical 16:9 PDF (Typst) + Marp + .pptx exports.',     'slides_gen',        'agent', 'apps/api/tools/toolkit (deck engine)'),
    ('rag_search',        'RAG Search',          'Retrieval over the user''s own imported material.',                  'rag_search',        'agent', 'apps/api/tools/rag_search_tool.py'),
    ('web_search',        'Web Search',          'Public-web search for current external information.',                'web_search',        'agent', 'apps/api/tools/web_search_tool.py'),
    ('read_document',     'Document Reading',    'Read the resolved document asset''s structured content.',            'read_document',     'agent', 'apps/api/tools/read_document_tool.py'),
    ('read_file',         'Local File Read',     'Read a workspace-local file.',                                       'read_file',         'agent', 'packages/agent/tools/fs_tools.py'),
    ('edit_file',         'Local File Edit',     'Edit a workspace-local file.',                                       'edit_file',         'agent', 'packages/agent/tools/fs_tools.py'),
    ('bash',              'Workspace Command',   'Run a sandboxed command in the workspace.',                          'bash',              'agent', 'packages/agent/tools/fs_tools.py (sandbox)'),
    ('translate',         'Translate',           'Translate text between languages.',                                  'translate',         'agent', 'apps/api/tools/translate_tool.py'),
    ('vision',            'Vision',              'Analyze an image (screenshot / chart / diagram).',                   'vision',            'agent', 'apps/api/tools/vision_tool.py'),
    ('pdf_table_to_text', 'PDF Table to Text',   'Convert tables inside the attached PDF to text.',                    'pdf_table_to_text', 'agent', 'apps/api/tools/pdf_tools.py'),
    ('research',          'Research',            'Multi-step Research OS project driver (plugin).',                    'research',          'agent', 'plugins/research'),
    ('artifact',          'Artifact Compile',    'Compile a research artifact to published PDF (plugin).',             'artifact',          'agent', 'plugins/artifact'),
    ('social_search',     'Social Search',       'Social-platform search (reddit & co., plugin).',                     'search_social',     'agent', 'plugins/social_search')
ON CONFLICT (action_key) DO NOTHING;
