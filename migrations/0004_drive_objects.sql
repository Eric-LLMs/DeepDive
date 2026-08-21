-- 0004_drive_objects.sql: per-user cloud drive + shared RAG corpus.
--
-- The content layer so far is global (materials/chunks have no user_id and are dead
-- tables — nothing writes them). This migration replaces that with a proper
-- tenant-isolated design:
--
--   global_objects   — one physical file per SHA-256 (dedup / instant upload), ref-counted
--   workspaces       — user-owned grouping; workspace membership is the sharing mechanism
--   assets           — one logical file per user/workspace, pointing at a global_object
--   asset_acl        — asset-level sharing (specified user, or public)
--   upload_sessions  — chunked upload + resume state
--   chunks           — RAG chunks rebuilt with user_id/workspace_id/asset_id for filtering
--   jobs.user_id     — attribute async jobs to the user who started them
--
-- Idempotent: statements are IF NOT EXISTS / guarded, so re-running is safe.

-- ── Physical layer: one row per unique SHA-256, shared across all users ──
CREATE TABLE IF NOT EXISTS global_objects (
    sha256      TEXT PRIMARY KEY,              -- pure 64-hex digest, no prefix
    size        BIGINT NOT NULL,
    storage_key TEXT NOT NULL,                 -- {root}/objects/{sha[0:2]}/{sha[2:4]}/{sha}
    mime_type   TEXT,
    ref_count   BIGINT NOT NULL DEFAULT 0,     -- number of logical assets pointing here
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Workspaces: user-owned groups; membership = sharing ──
CREATE TABLE IF NOT EXISTS workspaces (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS workspaces_owner_idx ON workspaces (owner_id);

CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role         TEXT NOT NULL,                -- 'owner' | 'editor' | 'viewer'
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);

CREATE INDEX IF NOT EXISTS workspace_members_user_idx ON workspace_members (user_id);

-- ── Logical layer: one asset per user/workspace, pointing at a physical object ──
CREATE TABLE IF NOT EXISTS assets (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id),        -- owner
    workspace_id  UUID REFERENCES workspaces(id),            -- nullable: future extension
    object_sha256 TEXT REFERENCES global_objects(sha256),    -- set once upload completes
    name          TEXT NOT NULL,
    folder_path   TEXT,
    mime_type     TEXT,
    size          BIGINT,
    file_status   TEXT NOT NULL DEFAULT 'uploading',  -- UPLOADING/PROCESSING/READY/DELETED
    rag_status    TEXT NOT NULL DEFAULT 'pending',    -- PENDING/PARSING/CHUNKING/EMBEDDING/INDEXED/FAILED
    meta          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS assets_user_idx         ON assets (user_id);
CREATE INDEX IF NOT EXISTS assets_workspace_idx    ON assets (workspace_id);
CREATE INDEX IF NOT EXISTS assets_object_idx       ON assets (object_sha256);
CREATE INDEX IF NOT EXISTS assets_user_deleted_idx ON assets (user_id, deleted_at);

-- ── Asset-level ACL: explicit sharing of a single asset (public = grantee NULL) ──
CREATE TABLE IF NOT EXISTS asset_acl (
    asset_id        UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    grantee_user_id UUID REFERENCES users(id) ON DELETE CASCADE,  -- NULL = public link
    permission      TEXT NOT NULL,               -- 'read' | 'write'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, grantee_user_id)
);

-- One public ACL per asset (unique index ignores the NULL grantee); the PK above
-- already covers non-null grantees.
CREATE UNIQUE INDEX IF NOT EXISTS asset_acl_public_uniq
    ON asset_acl (asset_id) WHERE grantee_user_id IS NULL;

-- ── Chunked upload sessions (resume tracking) ──
CREATE TABLE IF NOT EXISTS upload_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    asset_id        UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    sha256          TEXT NOT NULL,               -- expected final digest from the client
    size            BIGINT NOT NULL,
    chunk_size      INTEGER NOT NULL,
    num_chunks      INTEGER NOT NULL,
    received_chunks JSONB NOT NULL DEFAULT '[]', -- boolean array: index -> chunk received
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending/uploading/completed/failed/aborted
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS upload_sessions_asset_idx ON upload_sessions (asset_id);
CREATE INDEX IF NOT EXISTS upload_sessions_user_idx  ON upload_sessions (user_id);

-- ── Jobs attribution: which user enqueued the job ──
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS jobs_user_idx ON jobs (user_id);

-- ── Rebuild RAG chunks with tenant columns (old tables are dead: nothing writes them) ──
DROP TABLE IF EXISTS chunks CASCADE;
DROP TABLE IF EXISTS materials CASCADE;

CREATE TABLE chunks (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_id     UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    user_id      UUID,                           -- denormalized for filter efficiency
    workspace_id UUID,
    seq          INTEGER NOT NULL,
    content_en   TEXT NOT NULL,
    content_cn   TEXT,
    meta         JSONB NOT NULL DEFAULT '{}',
    embedding    vector(1024) NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_asset_idx     ON chunks (asset_id);
CREATE INDEX IF NOT EXISTS chunks_user_idx      ON chunks (user_id);
CREATE INDEX IF NOT EXISTS chunks_workspace_idx ON chunks (workspace_id);

-- HNSW approximate vector index. If the corpus stays small (< ~100k rows) or build
-- memory is a concern, this can be replaced with IVFFlat (lists=100) — exact cosine scan
-- is also acceptable at small scale.
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
