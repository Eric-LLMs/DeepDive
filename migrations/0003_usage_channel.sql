-- 0003_usage_channel.sql: record which LLM channel (credential) served each usage row.
--
-- User billing/stats stay keyed on the catalog model price; this column only records the
-- serving channel so the admin can aggregate cost by channel (business need).
--
-- Idempotent: statements are IF NOT EXISTS / guarded, so re-running is safe.

ALTER TABLE user_usage_logs
    ADD COLUMN IF NOT EXISTS credential_id UUID REFERENCES llm_credentials(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS user_usage_logs_cred_idx
    ON user_usage_logs (credential_id);
