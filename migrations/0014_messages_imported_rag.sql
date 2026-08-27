-- Per-message RAG import state. Marks whether a chat message's content is already in the
-- query repository, carried on the message row itself so the session fetch
-- (GET /sessions/{id}) renders the persistent "✓ Imported" state directly. Deriving it from
-- chunk meta breaks when a message is deleted or regrouped (the state spread to sibling
-- pairs and allowed duplicate single-pair imports); an explicit column survives deletes and
-- survives the whole-session rebuild. Default 0 = not imported; set to 1 on RAG import.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS imported_rag BOOLEAN NOT NULL DEFAULT FALSE;
