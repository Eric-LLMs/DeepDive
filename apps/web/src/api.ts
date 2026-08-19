// Thin fetch wrapper around the DeepDive REST API.
// In dev, Vite proxies /api/* to http://localhost:8300 (see vite.config.ts).
import type { Domain, JobId, JobInfo, Me, Sentence, Term } from "./types";

const BASE = "/api";
const TOKEN_KEY = "deepdive_token";

// Shared session token. The desktop client hands it over via ?sso=<token> on the
// web console URL; direct visits can sign in through the login page instead.
export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${BASE}${path}`, {
    headers,
    ...init,
  });
  if (res.status === 401) clearToken();
  if (!res.ok) throw new Error(await errorMessage(res));
  return (await res.json()) as T;
}

// Variant for bodies the browser must encode itself (multipart FormData): no
// Content-Type header, so fetch sets the correct multipart boundary.
async function requestRaw<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${BASE}${path}`, { headers, ...init });
  if (res.status === 401) clearToken();
  if (!res.ok) throw new Error(await errorMessage(res));
  return (await res.json()) as T;
}

async function errorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") return body.detail;
  } catch { /* not JSON */ }
  const text = await res.text().catch(() => "");
  return `${res.status} ${res.statusText}: ${text}`;
}

export interface LoginResponse {
  access_token: string;
  username: string;
  display_name: string | null;
  role_id: string;
  role_name: string;
}

// Common response of the account-action endpoints (register / resend / forgot /
// reset / PATCH me). debug_verify_url appears when SMTP is not configured.
export interface AuthActionResponse {
  status: string;
  message: string;
  debug_verify_url?: string;
  email_error?: string;
}

export const api = {
  // Auth
  login: (username: string, password: string) =>
    request<LoginResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  me: () => request<Me>("/auth/me"),
  register: (username: string, email: string, password: string, displayName?: string) =>
    request<AuthActionResponse>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, email, password, display_name: displayName }),
    }),
  forgotPassword: (email: string) =>
    request<AuthActionResponse>("/auth/forgot-password", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  resetPassword: (token: string, password: string) =>
    request<AuthActionResponse>("/auth/reset-password", {
      method: "POST",
      body: JSON.stringify({ token, password }),
    }),
  updateProfile: (patch: {
    display_name?: string | null;
    username?: string | null;
    email?: string | null;
    phone?: string | null;
    current_password?: string;
    new_password?: string;
  }) =>
    request<AuthActionResponse>("/auth/me", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  uploadAvatar: (form: FormData) =>
    requestRaw<{ avatar: string }>("/auth/me/avatar", { method: "POST", body: form }),

  // Domains
  listDomains: () => request<Domain[]>("/domains"),
  createDomain: (name: string) =>
    request<Domain>("/domains", { method: "POST", body: JSON.stringify({ name }) }),

  // Terms
  listTerms: (domainId: string) => request<Term[]>(`/domains/${domainId}/terms`),
  createTerm: (domainId: string, word: string, definition = "") =>
    request<Term>("/terms", {
      method: "POST",
      body: JSON.stringify({ domain_id: domainId, word, definition }),
    }),
  updateTerm: (termId: string, patch: Partial<Term>) =>
    request<{ status: string }>("/terms/update", {
      method: "POST",
      body: JSON.stringify({ term_id: termId, ...patch }),
    }),
  bulkUpdateTerms: (updates: { term_id: string; word?: string; definition?: string | null; star_level?: number; is_active?: boolean; frequency?: number }[]) =>
    request<{ status: string }>("/terms/bulk-update", {
      method: "POST",
      body: JSON.stringify({ updates }),
    }),

  // Bulk import
  importTerms: (domainId: string, text: string) =>
    request<{ added: number; skipped: number }>("/terms/import", {
      method: "POST",
      body: JSON.stringify({ domain_id: domainId, text }),
    }),
  importTermsStructured: (
    domainId: string,
    items: { word: string; definition?: string; frequency?: number; star_level?: number }[]
  ) =>
    request<{ added: number; skipped: number }>("/terms/import-structured", {
      method: "POST",
      body: JSON.stringify({ domain_id: domainId, items }),
    }),
  importSentences: (domainId: string, text: string) =>
    request<{ added: number; skipped: number }>("/sentences/import", {
      method: "POST",
      body: JSON.stringify({ domain_id: domainId, text }),
    }),
  importSentencesStructured: (domainId: string, items: string[]) =>
    request<{ added: number; skipped: number }>("/sentences/import-structured", {
      method: "POST",
      body: JSON.stringify({ domain_id: domainId, items }),
    }),

  // Images (enqueued, poll GET /jobs/{id})
  enqueueImageFetch: (word: string, definition: string, context: string, regenerate: boolean) =>
    request<JobId>("/image-fetch", {
      method: "POST",
      body: JSON.stringify({ word, definition, context, regenerate }),
    }),

  // Sentences
  createSentence: (domainId: string, content_en: string) =>
    request<Sentence>("/sentences", {
      method: "POST",
      body: JSON.stringify({ domain_id: domainId, content_en }),
    }),
  updateSentence: (sentenceId: string, patch: { content_cn?: string; audio_hash?: string }) =>
    request<{ status: string }>("/sentences/update", {
      method: "POST",
      body: JSON.stringify({ sentence_id: sentenceId, ...patch }),
    }),
  listSentences: (domainId: string) => request<Sentence[]>(`/domains/${domainId}/sentences`),
  searchSentences: (domainId: string, q: string) =>
    request<Sentence[]>(`/domains/${domainId}/sentences/search?q=${encodeURIComponent(q)}`),
  semanticSearch: (domainId: string, q: string) =>
    request<Sentence[]>(`/domains/${domainId}/sentences/semantic?q=${encodeURIComponent(q)}`),
  enqueueIndexSentences: (domainId: string) =>
    request<JobId>(`/domains/${domainId}/sentences/index`, { method: "POST" }),

  // Matches / relations
  linkTermToSentence: (termId: string, sentenceId: string, explanation?: string) =>
    request<{ status: string }>("/matches", {
      method: "POST",
      body: JSON.stringify({ term_id: termId, sentence_id: sentenceId, explanation }),
    }),
  listSentencesForTerm: (termId: string) => request<Sentence[]>(`/terms/${termId}/sentences`),

  // TTS (enqueued, poll GET /jobs/{id})
  enqueueTts: (text: string) => request<JobId>("/tts", {
    method: "POST",
    body: JSON.stringify({ text }),
  }),

  // AI capabilities (enqueued, poll GET /jobs/{id})
  enqueueGenerateDefinition: (term: string) =>
    request<JobId>("/terms/definition", {
      method: "POST",
      body: JSON.stringify({ term }),
    }),
  enqueueExplain: (term: string, context: string) =>
    request<JobId>("/explain", {
      method: "POST",
      body: JSON.stringify({ term, context }),
    }),
  enqueueAnalyzeSyntax: (sentence: string) =>
    request<JobId>("/sentences/analyze", {
      method: "POST",
      body: JSON.stringify({ sentence }),
    }),

  // Jobs
  getJob: (jobId: string) => request<JobInfo>(`/jobs/${jobId}`),
};
