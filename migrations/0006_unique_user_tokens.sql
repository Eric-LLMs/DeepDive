-- 0006_unique_user_tokens: one row per (user, pinned channel); re-login updates in place.
--
-- Before this, every login minted a fresh access_tokens row, so heavy logins grew the
-- table unboundedly. Now user tokens are reused per (user, credential_id):
--   * dedupe historical rows (keep the newest per pair) before adding the guard
--   * a partial unique index on (user_id, credential_id) where both are non-null
--     prevents duplicates; admin tokens (user_id NULL) and manual user tokens
--     (credential_id NULL) stay exempt, and the login endpoint reuses those rows
--     in application code instead.
--
-- One idempotent unit (final shape in a single pass).

-- ── 1. Dedupe existing login tokens: keep the newest row per (user, channel) ──
DELETE FROM access_tokens a
USING access_tokens b
WHERE a.user_id IS NOT NULL
  AND a.user_id = b.user_id
  AND a.credential_id IS NOT DISTINCT FROM b.credential_id
  AND (a.created_at < b.created_at
       OR (a.created_at = b.created_at AND a.id < b.id));

-- ── 2. Guard: at most one row per (user, pinned channel) ──
CREATE UNIQUE INDEX IF NOT EXISTS access_tokens_user_credential_uniq
    ON access_tokens (user_id, credential_id)
    WHERE user_id IS NOT NULL AND credential_id IS NOT NULL;
