-- Workspace / drive audit trail: who did what, when, on which target.
--
-- Columns are denormalized and intentionally have NO foreign keys, so an entry
-- survives the deletion of the workspace, actor, or target it references (audit
-- data outlives the rows it describes).

CREATE TABLE IF NOT EXISTS workspace_activity (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  UUID,               -- NULL = personal (My Drive) scope
    actor_user_id UUID,               -- NULL = system (e.g. retention sweep)
    actor_username TEXT,
    action        TEXT NOT NULL,      -- e.g. file.create / file.rename / member.add ...
    target_type   TEXT NOT NULL,      -- file | folder | member | workspace
    target_id     TEXT,
    target_name   TEXT,               -- file name / folder path / username / workspace name
    detail        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ws_activity_ws_created_idx ON workspace_activity (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ws_activity_actor_idx ON workspace_activity (actor_username);
CREATE INDEX IF NOT EXISTS ws_activity_target_idx ON workspace_activity (target_name);
