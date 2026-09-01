-- Per-message owned-attachment link. A chat screenshot the user captured (📷) is created
-- FOR the message that carries it, so the row records which asset that message owns. Deleting
-- the message/session cascades a soft-delete of that asset (folder-agnostic: the folder is
-- only UI organization). Attachments that merely *reference* an existing drive file (🔗 or
-- local-file attach) do NOT populate this column, so deleting such a message never touches
-- the referenced document. ON DELETE SET NULL keeps the FK safe if an asset is purged first.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS attach_asset_id UUID REFERENCES assets(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_messages_attach_asset ON messages(attach_asset_id);
