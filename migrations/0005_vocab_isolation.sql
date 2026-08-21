-- 0005_vocab_isolation.sql: per-user vocabulary (public + private mix).
--
-- The learning corpus (domains/terms/sentences) was global. This migration adds
-- ``user_id`` to all three tables: NULL = public/shared, a UUID = private to that owner.
-- Unique constraints become partial: public rows stay globally unique by the natural key,
-- private rows are unique per (user_id, natural key), so two users may each own the same
-- sentence/domain name without colliding.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS / guarded statements, so re-running is safe.

-- ── Add ownership columns (NULL = public) ──
ALTER TABLE domains   ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE terms     ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE sentences ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS domains_user_idx   ON domains (user_id);
CREATE INDEX IF NOT EXISTS terms_user_idx     ON terms (user_id);
CREATE INDEX IF NOT EXISTS sentences_user_idx ON sentences (user_id);

-- ── Replace the global unique constraints with partial unique indexes ──
-- domains.name: unique among public; unique per owner among private.
ALTER TABLE domains DROP CONSTRAINT IF EXISTS domains_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS domains_name_public_uniq
    ON domains (name) WHERE user_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS domains_name_private_uniq
    ON domains (user_id, name) WHERE user_id IS NOT NULL;

-- sentences.content_en: unique among public; unique per owner among private
-- (without this split, two users could never own the same sentence).
ALTER TABLE sentences DROP CONSTRAINT IF EXISTS sentences_content_en_key;
CREATE UNIQUE INDEX IF NOT EXISTS sentences_content_en_public_uniq
    ON sentences (content_en) WHERE user_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS sentences_content_en_private_uniq
    ON sentences (user_id, content_en) WHERE user_id IS NOT NULL;
