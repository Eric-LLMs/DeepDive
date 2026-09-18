-- 0016: per-message metadata (JSONB).
--
-- First use: the retrieval-feedback loop. When a chat answer was grounded on rag_search
-- hits, the assistant message stores a compact snapshot ``{"retrieval": {"hits":
-- [{id, score, text?}], "queries": [..]}}`` so the client can offer a persistent 👍/👎
-- rating on the message (recorded into ``rag_feedback`` via POST /rag/feedback).
-- Generic namespace — other per-message facts may ride along later.

ALTER TABLE messages ADD COLUMN IF NOT EXISTS meta JSONB;
