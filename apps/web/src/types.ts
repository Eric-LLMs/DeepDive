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
