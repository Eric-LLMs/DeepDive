// Thin fetch wrapper around the DeepDive REST API.
// In dev, Vite proxies /api/* to http://localhost:8300 (see vite.config.ts).
import type {
  Domain,
  DriveFile,
  DriveFolder,
  InitUploadResult,
  JobId,
  JobInfo,
  Me,
  Model,
  Sentence,
  ShareEntry,
  Term,
  UsageReport,
  Workspace,
  WorkspaceActivity,
  WorkspaceMember,
  WorkspaceUser,
} from "./types";

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

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
    ...init,
  });
  if (res.status === 401) clearToken();
  if (!res.ok) throw new Error(await errorMessage(res));
  return (await res.json()) as T;
}

// Variant for bodies the browser must encode itself (multipart FormData / raw bytes):
// no Content-Type header, so fetch sets the correct multipart boundary.
async function requestRaw<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: authHeaders(), ...init });
  if (res.status === 401) clearToken();
  if (!res.ok) throw new Error(await errorMessage(res));
  return (await res.json()) as T;
}

// Download endpoint returns raw bytes (StreamingResponse), not JSON — fetch the blob
// with the auth header so the caller can save or open it.
async function downloadBlob(path: string): Promise<Blob> {
  const res = await fetch(`${BASE}${path}`, { headers: authHeaders() });
  if (res.status === 401) clearToken();
  if (!res.ok) throw new Error(await errorMessage(res));
  return res.blob();
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
  // Stateless web-console sessions (cc_): unlike the /auth/login API token, which the
  // next login rotates out, a console session survives desktop re-logins. The console
  // signs in with sessionLogin and converts any hand-me-down API token (desktop SSO)
  // into one on mount via exchangeSession.
  sessionLogin: (username: string, password: string) =>
    request<LoginResponse>("/auth/session-login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  exchangeSession: () => request<LoginResponse>("/auth/session", { method: "POST" }),
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

  // Self-service usage / wallet report (own data only; optional paging/filter params)
  usage: (params?: Record<string, string | number>) =>
    request<UsageReport>(
      "/auth/usage" +
        (params ? "?" + new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])) : "")
    ),

  // Model catalog (for clicking a model name in the usage log to view its detail).
  models: () => request<{ models: Model[] }>("/auth/models"),

  // ── Cloud drive: files ──
  listFiles: () => request<{ files: DriveFile[] }>("/files"),
  getFile: (assetId: string) => request<DriveFile>(`/files/${assetId}`),
  getFileContent: (assetId: string) =>
    request<{ content: string }>(`/files/${assetId}/content`),
  updateFileContent: (assetId: string, content: string) =>
    request<{ asset: DriveFile; job_id?: string }>(`/files/${assetId}/content`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),
  initUpload: (body: {
    sha256: string;
    size: number;
    name: string;
    folder_path?: string | null;
    mime_type?: string | null;
    workspace_id?: string | null;
  }) =>
    request<InitUploadResult>("/files/init-upload", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  uploadChunk: (assetId: string, index: number, blob: Blob) =>
    requestRaw<{ ok: boolean; index: number }>(`/files/${assetId}/chunks/${index}`, {
      method: "PUT",
      body: blob,
    }),
  chunkStatus: (assetId: string) =>
    request<{ session_id: string; received: number[]; missing: number[]; chunk_size: number; num_chunks: number }>(
      `/files/${assetId}/chunks`
    ),
  completeUpload: (assetId: string) =>
    request<{ asset?: DriveFile | null; job_id?: string }>(`/files/${assetId}/complete`, {
      method: "POST",
    }),
  abortUpload: (assetId: string) =>
    request<{ aborted: boolean }>(`/files/${assetId}/abort`, { method: "POST" }),
  deleteFile: (assetId: string) =>
    request<{ deleted: boolean; physical_removed: boolean }>(`/files/${assetId}`, {
      method: "DELETE",
    }),
  renameFile: (assetId: string, body: { name?: string; folder_path?: string | null }) =>
    request<DriveFile>(`/files/${assetId}`, { method: "PATCH", body: JSON.stringify(body) }),
  downloadFile: (assetId: string) => downloadBlob(`/files/${assetId}/download`),
  ingestStatus: (assetId: string) =>
    request<{ asset_id: string; file_status: string; rag_status: string }>(
      `/files/${assetId}/ingest-status`
    ),
  moveFile: (assetId: string, body: { workspace_id?: string | null; folder_path: string | null }) =>
    request<DriveFile>(`/files/${assetId}/move`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ── Cloud drive: folders ──
  listFolders: () => request<{ folders: DriveFolder[] }>("/folders"),
  createFolder: (body: {
    name: string;
    parent_path?: string | null;
    workspace_id?: string | null;
  }) =>
    request<DriveFolder>("/folders", { method: "POST", body: JSON.stringify(body) }),
  renameFolder: (folderId: string, body: { name: string }) =>
    request<DriveFolder>(`/folders/${folderId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteFolder: (folderId: string) =>
    request<{ deleted: boolean; folder_id: string }>(`/folders/${folderId}`, {
      method: "DELETE",
    }),

  // ── Cloud drive: trash ──
  listTrash: () => request<{ files: DriveFile[] }>("/trash"),
  restoreTrash: (assetId: string) =>
    request<DriveFile>(`/trash/${assetId}/restore`, { method: "POST" }),
  purgeTrash: (assetId: string) =>
    request<{ purged: boolean; physical_removed: boolean }>(`/trash/${assetId}`, {
      method: "DELETE",
    }),
  emptyTrash: () => request<{ purged: number }>("/trash", { method: "DELETE" }),

  // ── Cloud drive: sharing ──
  shareFile: (assetId: string, body: { grantee_user_id?: string | null; permission: string }) =>
    request<{ asset_id: string; grantee_user_id: string | null; permission: string }>(
      `/files/${assetId}/share`,
      { method: "POST", body: JSON.stringify(body) }
    ),
  unshareFile: (assetId: string, grantee: string) =>
    request<{ removed: boolean }>(`/files/${assetId}/share/${grantee}`, { method: "DELETE" }),
  listShares: (assetId: string) =>
    request<{ shares: ShareEntry[] }>(`/files/${assetId}/shares`),

  // ── Cloud drive: workspaces + members ──
  listWorkspaces: () => request<{ workspaces: Workspace[] }>("/workspaces"),
  createWorkspace: (name: string) =>
    request<Workspace>("/workspaces", { method: "POST", body: JSON.stringify({ name }) }),
  renameWorkspace: (workspaceId: string, name: string) =>
    request<{ id: string; name: string }>(`/workspaces/${workspaceId}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),
  deleteWorkspace: (workspaceId: string) =>
    request<{ deleted: boolean; workspace_id: string }>(`/workspaces/${workspaceId}`, {
      method: "DELETE",
    }),
  listWorkspaceMembers: (workspaceId: string) =>
    request<{ members: WorkspaceMember[] }>(`/workspaces/${workspaceId}/members`),
  addWorkspaceMember: (workspaceId: string, body: { user_id: string; role: string }) =>
    request<{ user_id: string; role: string }>(`/workspaces/${workspaceId}/members`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateWorkspaceMember: (workspaceId: string, memberId: string, role: string) =>
    request<{ user_id: string; role: string }>(`/workspaces/${workspaceId}/members/${memberId}`, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    }),
  removeWorkspaceMember: (workspaceId: string, memberId: string) =>
    request<{ removed: boolean }>(`/workspaces/${workspaceId}/members/${memberId}`, { method: "DELETE" }),

  // Resolve a username / user-id fragment to users so members can be added by name.
  searchUsers: (q: string, limit = 10) =>
    request<{ users: WorkspaceUser[] }>(
      `/users/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),

  // Page the workspace audit trail; filters: fuzzy q (actor/target), start/end dates.
  listWorkspaceActivity: (
    workspaceId: string,
    params: { q?: string; start?: string; end?: string; limit?: number; offset?: number },
  ) => {
    const sp = new URLSearchParams();
    if (params.q) sp.set("q", params.q);
    if (params.start) sp.set("start", params.start);
    if (params.end) sp.set("end", params.end);
    sp.set("limit", String(params.limit ?? 20));
    sp.set("offset", String(params.offset ?? 0));
    return request<{ total: number; items: WorkspaceActivity[] }>(
      `/workspaces/${workspaceId}/activity?${sp.toString()}`,
    );
  },
};
