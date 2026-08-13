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

export interface ExplainResult {
  translation: string;
  explanation: string;
}
