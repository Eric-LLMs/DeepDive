# 01 — Product Specification

> **Normative.** Behavior not explicitly permitted here is prohibited by default. See
> `00-architecture-overview.md` for the governing principle and success criterion.

## 1. Mission

DeepDive Research OS gives a user a persistent, governed, reproducible research project
inside their existing DeepDive workspace — without leaving chat. It turns:

> an idea, a question, a paper, a dataset, an observation

into:

> a defensible research question → evidence → validated findings → bounded claims →
> a deliverable (memo, literature review, research report, paper, replication report) →
> durable files in the cloud drive → searchable via the user's RAG corpus.

It optimizes for **defensible research**, not apparent completion. A polished report with
no evidence chain is a failure.

## 2. Personas

| Persona | Goal | Typical request |
|---|---|---|
| **Learner** | Understand a topic deeply and produce study material | "Summarize the state of research on RAG agents" → literature review |
| **Researcher** | Write a rigorous, reproducible empirical study | "Conduct an empirical study on X using the data I uploaded" |
| **Professional** | Produce a decision-oriented report | "Research the TCO of self-hosted LLM gateways" → research report |
| **Team** | Share a research workspace (future) | "Open this project to my collaborator" |

## 3. Entry points

1. **Research Inbox** — capture-then-promote: "这个想法以后可以研究一下" records an
   inbox item; the user (or agent) promotes it into a `ResearchProject`.
2. **Explicit project** — the user names a topic, question, or deliverable and asks for
   research.
3. **Resume / open** — continue or inspect an existing project from a dashboard.

## 4. Output types (Output Profile)

| Output | What is delivered | Typical stage path |
|---|---|---|
| `memo` | Short scoped write-up with sources | DISCOVER → FRAME → EVIDENCE → WRITE |
| `literature_review` | Survey + synthesis matrix, citations verified | DISCOVER → FRAME → EVIDENCE → WRITE → REVIEW |
| `research_report` | Structured report with findings, claims, risks | DISCOVER → FRAME → EVIDENCE → EXECUTE → EXPLAIN → WRITE → REVIEW |
| `proposal` | Research plan: question, design, data plan | DISCOVER → FRAME → EVIDENCE → DESIGN |
| `paper` | Full manuscript, claim-governed, reproducible | full pipeline + REPRODUCE |
| `replication_report` | Reproduction/validation report of prior work | EVIDENCE → EXECUTE → REPRODUCE |

## 5. Method profiles

`literature` · `empirical` · `theoretical` · `qualitative` · `mixed`.

Method × Output is a **two-dimensional profile** (see `09-research-profiles.md`):
"how we research" and "what we deliver" are orthogonal. `empirical × paper` = an empirical
study; `literature × literature_review` = a review; `mixed × memo` = a research memo.

## 6. Non-goals (explicit)

- Research OS does **not** replace DeepDive's chat/learning loop; it is a governed mode
  layered on top of the same agent kernel.
- It does **not** provide a statistics execution service in MVP (sandbox execution is
  Phase 2). Analysis runs as structured reasoning, and — where a profile permits — Python
  via the existing `bash` sandbox.
- It does **not** auto-submit to journals; publication is a human action.
- It does **not** silently change designs or self-approve gate overrides (see `11`).

## 7. Acceptance criteria for the product

1. A user can go from a chat idea to a research project with a drive folder in one session.
2. Every deliverable claim traces to an evidence record (Claim → Evidence → Result →
   Script/Data) or is marked unverified.
3. Every run that costs compute leaves an immutable `ResearchExecution` record.
4. No high-risk change (question, design, sample, estimator, claim strength, data source,
   publication) happens without explicit human approval.
5. A completed project is reproducible: provenance documented, `run_all.sh` + environment
   + data notes produced where the profile requires it.

## 8. MVP product scope

The MVP (Phase 1) is the shortest defensible loop (see `17-mvp-scope.md`):

```text
Research Inbox → Project → DISCOVER → FRAME → EVIDENCE → SYNTHESIZE
→ EVIDENCE GATE → Research Report → Cloud Drive → RAG
```

Profiles: `literature` / `mixed` × `memo` / `literature_review` / `research_report`.
Empirical rigor, claim anchoring, peer-review simulation, and reproducibility packs are
Phase 2/3, designed for but not implemented in the MVP.
