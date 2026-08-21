-- 0006_folders.sql: first-class folders + trash-retention search indexes.
--
--   folders   — one row per folder path within a scope. A folder is scoped to a
--               workspace (workspace_id set) or to a user's personal drive
--               (workspace_id NULL). The path is the full '/'-separated relative
--               path inside that scope, so multi-level trees need no parent FK:
--               ancestors are implicit. Shared workspace folders are visible to
--               every member (user_id records the creator only).
--
-- Also adds the name/folder_path indexes the file-name search needs.
--
-- Idempotent: IF NOT EXISTS / guarded, re-running is safe.

CREATE TABLE IF NOT EXISTS folders (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,  -- NULL = personal (My Drive)
    path         TEXT NOT NULL,                                     -- e.g. "English/Vocab"
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One folder per (workspace, path); Postgres treats NULLs as distinct, so the
-- personal scope needs its own partial unique index.
CREATE UNIQUE INDEX IF NOT EXISTS folders_unique_ws
    ON folders (workspace_id, path) WHERE workspace_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS folders_unique_personal
    ON folders (path) WHERE workspace_id IS NULL;

CREATE INDEX IF NOT EXISTS folders_user_idx ON folders (user_id);

-- File-name search support (case-insensitive prefix/substring) + folder browsing.
CREATE INDEX IF NOT EXISTS assets_name_idx   ON assets (lower(name));
CREATE INDEX IF NOT EXISTS assets_folder_idx ON assets (folder_path);
