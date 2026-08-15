-- 0001_init: initial schema.
--
-- Extensions first, then core tables. Idempotent (IF NOT EXISTS) so it is safe to re-run
-- against a database that was already created by the previous Alembic migration.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS domains (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS materials (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type        TEXT NOT NULL,               -- 'domain' | 'video' | 'document'
    title       TEXT NOT NULL,
    source_url  TEXT,
    meta        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS terms (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id   UUID NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    word        TEXT NOT NULL,
    definition  TEXT,
    frequency   INTEGER NOT NULL,
    star_level  INTEGER NOT NULL,
    audio_hash  TEXT,
    image_paths JSONB NOT NULL,
    is_active   BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS sentences (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id      UUID NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    origin_source  TEXT,
    content_en     TEXT NOT NULL UNIQUE,
    content_cn     TEXT,
    audio_hash     TEXT,
    cn_explanation TEXT,
    embedding      vector(1024)
);

CREATE TABLE IF NOT EXISTS chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    material_id UUID NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    content_en  TEXT NOT NULL,
    content_cn  TEXT,
    meta        JSONB NOT NULL,
    embedding   vector(1024) NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ,
    summary     TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,               -- 'user' | 'assistant' | 'tool'
    text        TEXT NOT NULL,
    embedding   vector(1024),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS session_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    type        TEXT NOT NULL,
    timestamp   DOUBLE PRECISION NOT NULL,
    payload     JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    term_id        UUID NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    sentence_id    UUID NOT NULL REFERENCES sentences(id) ON DELETE CASCADE,
    cn_explanation TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type         TEXT NOT NULL,
    status       TEXT NOT NULL,
    payload      JSONB NOT NULL,
    result       JSONB,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
