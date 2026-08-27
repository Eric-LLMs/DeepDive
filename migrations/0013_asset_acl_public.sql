-- Asset ACL: enable public-link sharing (NULL grantee). The original composite
-- PRIMARY KEY (asset_id, grantee_user_id) implicitly made grantee_user_id NOT NULL,
-- so Postgres rejected the NULL row the public-share path relies on (500 on
-- POST /files/{id}/share with grantee_user_id=null). Switch to a surrogate id PK;
-- uniqueness is re-enforced by partial indexes: one public row per asset (NULL
-- grantee) and one row per (asset_id, grantee) for named grantees.
ALTER TABLE asset_acl ADD COLUMN IF NOT EXISTS id UUID;
UPDATE asset_acl SET id = gen_random_uuid() WHERE id IS NULL;
ALTER TABLE asset_acl ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE asset_acl ALTER COLUMN id SET NOT NULL;
ALTER TABLE asset_acl DROP CONSTRAINT IF EXISTS asset_acl_pkey;
ALTER TABLE asset_acl ALTER COLUMN grantee_user_id DROP NOT NULL;
ALTER TABLE asset_acl ADD CONSTRAINT asset_acl_pkey PRIMARY KEY (id);
CREATE UNIQUE INDEX IF NOT EXISTS asset_acl_grantee_uniq
    ON asset_acl (asset_id, grantee_user_id) WHERE grantee_user_id IS NOT NULL;
