-- 0007_console_tokens_stateless: admin console logins no longer persist a token row.
--
-- The console now returns a signed, stateless session token (HMAC, held in the browser)
-- instead of minting an access_tokens row, so every browser login no longer grows the
-- table. This migration removes the leftover console-login rows (role='admin' with no
-- user and no channel, named after the console account) and hardens the
-- (user, channel) uniqueness guarantee for the "no channel pinned" case, which the
-- 0006 partial index intentionally did not cover.

-- ── 1. Drop leftover console-login rows ──
-- Console logins minted rows with role='admin', no user, no channel, and the console
-- username as the token name. Tokens-page admin API tokens (user-chosen names) are kept.
DELETE FROM access_tokens
WHERE role = 'admin'
  AND user_id IS NULL
  AND credential_id IS NULL
  AND name = (SELECT COALESCE(value->>'username', 'admin') FROM app_settings WHERE key = 'admin');

-- ── 2. Dedupe legacy (user, no-channel) rows: keep the newest per user ──
DELETE FROM access_tokens a
USING access_tokens b
WHERE a.user_id IS NOT NULL
  AND a.credential_id IS NULL
  AND a.user_id = b.user_id
  AND b.credential_id IS NULL
  AND (a.created_at < b.created_at
       OR (a.created_at = b.created_at AND a.id < b.id));

-- ── 3. Guard: at most one (user, no-channel) token per user ──
-- Complements 0006's index (which only covers pinned channels) so the uniqueness rule
-- holds for both cases: one row per (user, channel) whether the channel is pinned or not.
CREATE UNIQUE INDEX IF NOT EXISTS access_tokens_user_no_cred_uniq
    ON access_tokens (user_id)
    WHERE user_id IS NOT NULL AND credential_id IS NULL;
