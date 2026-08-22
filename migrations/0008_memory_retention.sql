-- Memory retention: daily cron sweeps the audit event log (session_events).
--
-- Only the audit log is purged; messages (the recall corpus) and sessions (summaries /
-- closed_at) are deliberately kept. session_events.timestamp is a Float epoch seconds, so a
-- plain B-tree index makes the range DELETE fast.

CREATE INDEX IF NOT EXISTS idx_session_events_timestamp ON session_events (timestamp);
