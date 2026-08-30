# 13 — Storage Model

> **Normative.** Three layers, **one-way flow**, no bidirectional sync:
>
> ```text
> Scratch (ephemeral)  →  Cloud Drive (durable Source of Record)  →  RAG (derived index)
> ```
>
> The **database** (research tables + graph) is the system Source of Truth for metadata,
> state, and lineage. The cloud drive is the durable Source of Record for promoted content.
> RAG is a derived projection and may never write back to the drive or the metadata.

## 1. Layer responsibilities

### 1.1 Runtime Scratch (cache)

- Location: `{research_scratch_dir}/{owner_id}/{project_id}/` (config
  `research_scratch_dir`, default `data/research_scratch`; may be pointed at `/tmp`).
- Contents: downloaded/raw text, intermediate CSV, sandbox logs, tool output, DRAFT
  artifacts, anything not yet a durable claim.
- Properties: **ephemeral and purgeable**. Never the basis for a claim. Not backed up.

### 1.2 Cloud Drive (durable Source of Record)

- Location: user's personal drive scope, folder `research/{project_id}/`.
- Contents: only `PROMOTED`/`FROZEN` milestone artifacts — corpus, sources (verified),
  design_register, results/exhibits, draft, FINAL_REPORT, replication pack.
- Written exclusively via `DriveService.save_artifact(user_id, name, mime_type, content,
  folder_path="research/<project_id>")`: per-user, SHA-256 deduped, ref-counted, shareable,
  RAG-ingestable.
- Properties: **authoritative and durable**. Deleting an asset is a separate, human-audited
  action and never silently removes the artifact record.

### 1.3 Research metadata / graph DB

- The `research_*` tables + `research_entities`/`research_links` are the system Source of
  Truth for state, lineage, and claims.
- The agent-facing `workflow_state.json` is a **cache** rendered from the DB, not the truth.

### 1.4 RAG (derived index)

- Built from promoted drive assets via the existing `asset_ingest` / retrieval capability.
- **Derived only**: drive delete → index removal; index can never mutate the drive or
  metadata.

## 2. One-way flow rules

1. Scratch → Drive only through `research_artifact promote_to_drive` (which validates
   checksum and sets `PROMOTED`).
2. Drive → RAG through ingest; nothing else.
3. No path writes drive bytes back into scratch as authoritative.
4. A `FROZEN` version is byte-immutable; edits create a new version.
5. Retention: scratch is purged on project `ARCHIVED` (or on demand); drive content follows
   the existing trash/retention policy.

## 3. Path mapping

| Logical artifact | Scratch path (ephemeral) | Drive path (durable) |
|---|---|---|
| corpus | `{scratch}/{project}/01_corpus.md` | `research/{project}/01_corpus.md` |
| design_register | `{scratch}/{project}/03_design_register.md` | `research/{project}/03_design_register.md` |
| draft | `{scratch}/{project}/05_draft.md` | `research/{project}/05_draft.md` |
| replication pack | `{scratch}/{project}/08_replication/` | `research/{project}/08_replication.zip` |
| FINAL_REPORT | `{scratch}/{project}/FINAL_REPORT.md` | `research/{project}/FINAL_REPORT.md` |

`storage_path` in the artifact record is the logical path relative to the storage scope;
physical asset names are managed by `save_artifact` (auto-uniqued).

## 4. Delete semantics

- Scratch delete: fine, ephemeral.
- Drive asset delete: sets the artifact `INVALID` (with reason) or `SUPERSEDED`; triggers
  RAG index removal; recorded in audit. Never a silent record removal.
- Project `ARCHIVED`: drive content kept (or purged per retention); scratch purged; graph
  kept read-only.

## 5. Multi-tenancy isolation

- All research rows are owner-scoped; reads filter by ownership and reuse
  `asset_visible_expr` for drive-backed content.
- Scratch roots are keyed by `owner_id` — no cross-tenant filesystem collision.
- The acting user is always `request_user` (ContextVar); never from client input.

## 6. Storage invariants

1. No `PROMOTED`/`FROZEN` artifact without its bytes present in the drive.
2. No RAG write path touches drive or metadata.
3. No silent bidirectional sync; the only flows are scratch→drive→(rag) and db→projections.
4. A project's durable record is always reconstructible from the DB + drive.
