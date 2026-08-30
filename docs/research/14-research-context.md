# 14 — Research Context (RAG Layer)

> **Normative.** RAG is the **derived knowledge index** of a research project. It is a
> projection — it never mutates the drive or the metadata, and it can never be the source
> of truth for a claim (the graph is).

## 1. The Research Context Layer

A research project draws context from several knowledge pools:

```text
Research Context
├── Personal Knowledge   — the user's drive corpus (existing rag_search over user assets)
├── Project Artifacts    — this project's promoted artifacts (indexed on promote)
├── Literature Corpus    — verified Source nodes / references.bib (project-scoped index)
├── External Web         — live web_search / search_social results (not persisted as fact)
└── Previous Research    — the user's prior research projects (future)
```

## 2. What gets indexed and when

| Pool | Source | Index trigger |
|---|---|---|
| Project artifacts | promoted drive assets | existing `asset_ingest` on promote |
| Personal knowledge | user's drive | existing (unchanged) |
| Literature corpus | `research_sources` verified + references | on PROMOTE/FROZEN of sources |
| External web | search results | not persisted; used transiently |

Rules:

- Only `PROMOTED`/`FROZEN` content is indexed; scratch/DRAFT is never indexed.
- Indexing is **derived**: a drive delete or artifact invalidation removes/descopes the
  index entries; the index never writes back.
- Retrieval of a project artifact's content for the agent is always done by reading the
  artifact (drive/scratch), not by trusting an RAG snippet as ground truth.

## 3. Context Router (evolution, designed now, built later)

For a query during research, the router aggregates ranked candidates:

```text
query
   ↓
Context Router
   ├── Project RAG (this project's promoted artifacts)   [highest priority]
   ├── Personal RAG (user drive)                          [scoped to user]
   ├── Literature corpus (verified sources)               [verification-filtered]
   └── External web (web_search / search_social)          [unverified, marked]
   ↓
evidence-ranked context
```

Routing and ranking rules:

- Project RAG results are weighted above personal RAG; personal above web.
- Web results carry `source_type=web_page/blog/...` + `verification_status=unverified`;
  they may inform, never serve as verified fact.
- The router is implemented as a **skill-level orchestration** over the existing
  `retrieval` capability (`rag_search` with `filters={"user_id": ...}` and optional
  `domain`/project scoping) plus `web_search`. It reuses `QueryRepository`/RRF — no parallel
  retrieval system.

## 4. Integration with DeepDive retrieval

- The existing `retrieval` capability already accepts `filters={"user_id": ...}` and an
  optional `domain` scope (see `apps/api/tools/rag_search_tool.py`). Project scoping adds a
  project filter keyed on the project's drive folder / domain id.
- Personal knowledge retrieval is unchanged; guests (`user_id is None`) only see
  public-link assets.
- In the MVP, `rag_search` (personal + project) + `web_search` are called directly by the
  stage skills; the Context Router is Phase 4.

## 5. Context invariants

1. The index is derived; no write-back to drive or metadata.
2. Only PROMOTED/FROZEN content is indexed.
3. A snippet is a recall aid, not evidence; claims reference artifact reads, not snippets.
4. Web/unverified content is always tagged and never promoted to `VALID` evidence by
   retrieval alone.
5. Deletion/invalidation descopes or removes index entries (drive delete → index removal).
