// Shared API types (mirror the FastAPI response models in apps/api/schemas.py).

export interface Domain {
  id: string;
  name: string;
  created_at?: string;
}

export interface Term {
  id: string;
  domain_id: string;
  word: string;
  definition?: string | null;
  frequency: number;
  star_level: number;
  audio_hash?: string | null;
  image_paths: string[];
  is_active: boolean;
}

export interface Sentence {
  id: string;
  domain_id: string;
  origin_source?: string | null;
  content_en: string;
  content_cn?: string | null;
  audio_hash?: string | null;
  cn_explanation?: string | null;
  score?: number;
}

// Authenticated user profile (mirrors GET /auth/me from apps/api/main.py).
export interface Me {
  user_id: string;
  username: string;
  display_name: string | null;
  email: string | null;
  phone: string | null;
  avatar: string | null;
  email_verified: boolean;
  role_id: string;
  role_name: string;
  quota?: {
    role_name?: string;
    daily_token_limit?: number;
    daily_request_limit?: number;
    rpm_limit?: number;
    default_model?: string;
  };
}

// Async job model (mirrors GET /jobs/{id} from apps/api/main.py).
export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "unknown";

export interface JobInfo<T = unknown> {
  status: JobStatus;
  result: T | null;
  error: string | null;
}

export interface JobId {
  job_id: string;
}

// Self-service usage report (mirrors GET /auth/usage from apps/api/main.py).
export interface UsageCounter {
  period_start: string;
  request_count: number;
  token_count: number;
}
export interface UsageLog {
  id: string;
  created_at: string | null;
  credential_name: string;
  model_name: string;
  tool: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
}
export interface WalletTx {
  id: string;
  type: string;
  amount: number;
  balance_after: number;
  description: string | null;
  created_at: string | null;
}
export interface UsageReport {
  balance: number;
  currency: string;
  counters: UsageCounter[];
  logs: UsageLog[];
  transactions: WalletTx[];
  total: number;
}

// Model catalog entry (mirrors GET /auth/models from apps/api/main.py — same masked
// shape as the admin /admin/models, but readable by any signed-in user).
export interface Model {
  id: string;
  name: string;
  provider_model_name: string | null;
  description: string | null;
  prompt_price_per_1k: number;
  completion_price_per_1k: number;
  is_active: boolean;
  created_at: string | null;
}

// ── Cloud drive (mirror the asset dicts from core.application.drive_service) ──
export interface DriveFile {
  id: string;
  user_id: string;
  workspace_id: string | null;
  object_sha256: string | null;
  name: string;
  folder_path: string | null;
  mime_type: string | null;
  size: number;
  file_status: string; // UPLOADING | PROCESSING | READY | DELETED
  rag_status: string; // PENDING | PARSING | CHUNKING | EMBEDDING | INDEXED | FAILED
  created_at: string | null;
  updated_at: string | null;
  deleted_at: string | null;
}

// First-class folder row (mirror core.application.drive_service._folder_dict).
export interface DriveFolder {
  id: string;
  user_id: string;
  workspace_id: string | null; // null = My Drive (personal)
  name: string;
  path: string; // full '/'-separated path within the workspace/personal scope
  created_at: string | null;
  updated_at: string | null;
}

export interface Workspace {
  id: string;
  name: string;
  owner_id: string;
  // The requesting user's role in this workspace: "owner" | "admin" | "editor" | "viewer".
  // Drives which action buttons the web console enables.
  role?: string;
  owner_username?: string | null;
  owner_display_name?: string | null;
}

// Workspace member row (mirrors GET /workspaces/{id}/members from apps/api/routers/drive.py).
// The owner is NOT included — the frontend prepends the owner row from workspace.owner_id.
export interface WorkspaceMember {
  user_id: string;
  role: string; // admin | editor | viewer
  username: string | null;
  display_name: string | null;
}

export interface ShareEntry {
  grantee_user_id: string | null; // null = public link
  permission: string; // read | write
}

// User found via GET /users/search (used to resolve a username to a UUID when adding
// members — the /members endpoint requires a UUID, not a display name).
export interface WorkspaceUser {
  user_id: string;
  username: string;
  display_name: string | null;
}

// One row of the workspace audit trail (mirrors GET /workspaces/{id}/activity from
// apps/api/routers/drive.py and core.application.drive_service._activity_dict).
export interface WorkspaceActivity {
  id: string;
  workspace_id: string | null;
  actor_user_id: string | null; // null = system (e.g. retention sweep)
  actor_username: string | null;
  action: string; // file.create / file.rename / member.add / workspace.delete ...
  target_type: string; // file | folder | member | workspace
  target_id: string | null;
  target_name: string | null;
  detail: string | null;
  created_at: string | null;
}

export interface InitUploadResult {
  status: "instant" | "uploading";
  dedup?: boolean;
  asset?: DriveFile;
  asset_id?: string;
  session_id?: string;
  chunk_size?: number;
  num_chunks?: number;
  received?: number[];
}
