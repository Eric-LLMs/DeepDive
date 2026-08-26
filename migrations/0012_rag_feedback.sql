-- RAG feedback: query → chunks → rating → reason (golden dataset for fine-tune/eval).
-- The desktop workbench rates the chunks behind an answer; each row snapshots the query,
-- the retrieved hits (ids + scores), and the rating/reason so eval can run offline.
CREATE TABLE IF NOT EXISTS rag_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    query TEXT NOT NULL,
    rating BOOLEAN NOT NULL,          -- TRUE = relevant (👍), FALSE = not (👎)
    reason TEXT NULL,
    hits JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{id, score, text?}]
    filters JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX rag_feedback_user_idx ON rag_feedback(user_id);
CREATE INDEX rag_feedback_rating_idx ON rag_feedback(rating);
