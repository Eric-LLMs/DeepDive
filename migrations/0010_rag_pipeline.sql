-- RAG pipeline: domain filter + parent/child (small-to-big) chunks + CJK keyword column.
-- P1: domain filtering exposes assets.domain_id to rag_search (opaque on links).
ALTER TABLE assets ADD COLUMN domain_id UUID NULL REFERENCES domains(id) ON DELETE SET NULL;
CREATE INDEX assets_domain_idx ON assets(domain_id);

-- P1: parent/child indexing. leaf chunks carry parent_chunk_id; recall searches leaf
-- only and parent_expand swaps a leaf hit for its parent's full text.
ALTER TABLE chunks ADD COLUMN parent_chunk_id UUID NULL REFERENCES chunks(id) ON DELETE SET NULL;
ALTER TABLE chunks ADD COLUMN chunk_kind TEXT NOT NULL DEFAULT 'leaf';
CREATE INDEX chunks_parent_idx ON chunks(parent_chunk_id);

-- P2: CJK keyword channel. jieba-tokenized segments are stored here and matched with a
-- GIN index over to_tsvector('simple', ...), keeping the English tsvector path unchanged.
ALTER TABLE chunks ADD COLUMN content_search TEXT NULL;
CREATE INDEX chunks_content_search_idx ON chunks USING GIN (to_tsvector('simple', COALESCE(content_search, '')));
