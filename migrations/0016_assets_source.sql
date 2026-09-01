-- Source-asset binding for RAG-derived image files. When a PDF/DOCX is ingested, its embedded
-- images are saved as cloud-drive assets that point back at the source document via
-- source_asset_id (their own content hash lives in object_sha256). This powers two behaviors:
--   1. dedupe on re-ingest — the same (source, content-hash) pair reuses the existing asset
--      instead of creating a duplicate row (index idx_assets_source_content);
--   2. lifecycle cascade — deleting the source document (soft -> trash, then hard purge)
--      cleans up its derived images (ON DELETE CASCADE is the hard-delete safety net; the
--      app-level purge also decrements refcounts correctly).
ALTER TABLE assets ADD COLUMN IF NOT EXISTS source_asset_id UUID REFERENCES assets(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS idx_assets_source_asset ON assets(source_asset_id);
CREATE INDEX IF NOT EXISTS idx_assets_source_content ON assets(source_asset_id, object_sha256);
