// Thin fetch wrapper around the DeepDive REST API.
// In dev, Vite proxies /api/* to http://localhost:8000 (see vite.config.ts).
import type { Domain, JobId, JobInfo, Sentence, Term } from "./types";

const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return (await res.json()) as T;
}

export const api = {
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
