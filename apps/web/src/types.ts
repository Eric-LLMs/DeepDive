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
