-- Query repository: unify RAG content from three sources (file / learning / chat).
-- Chunks keep the file link (asset_id) but gain a source discriminator so non-file
-- content (learning sentences & articles, chat Q&A) lives in the same searchable table.
ALTER TABLE chunks ALTER COLUMN asset_id DROP NOT NULL;  -- non-file sources have no asset
ALTER TABLE chunks ADD COLUMN source_type TEXT NOT NULL DEFAULT 'file';  -- file | learning | chat
ALTER TABLE chunks ADD COLUMN source_id TEXT NULL;  -- article_id / sentence_id / session msg pair
CREATE INDEX chunks_source_idx ON chunks(source_type);

-- New "article" entity for the Learning Platform: free-text study material that can be
-- pushed into the query repository alongside sentences and drive files.
CREATE TABLE articles (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  domain_id  UUID NULL REFERENCES domains(id) ON DELETE SET NULL,
  title      TEXT NOT NULL,
  content    TEXT NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
