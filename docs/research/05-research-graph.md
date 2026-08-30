# 05 — ResearchGraph Contract

> **Normative.** Supersedes `02-domain-model.md` §2.3 where more specific.

## 1. The graph is Project-scoped

- Every node and edge carries `project_id`. **No cross-project edges exist.**
- All graph operations are scoped `WHERE project_id = ?`; a project cannot read or mutate
  another project's graph.
- The graph is the **system Source of Truth** for lineage. Human-readable markdown
  (evidence_ledger.md, claim_registry.md) is a **projection** of the graph, not a store.

## 2. Node types (normative set)

`Question, Hypothesis, Source, Dataset, Variable, Design, Execution, Analysis, Result,
Evidence, Claim, Table, Figure, Paragraph, Decision, Risk, Gate`.

`Execution` is a first-class node **and** a `ResearchExecution` record: the canonical chain is

```text
Dataset ──transformed_by──▶ Execution ──generated_by──▶ Analysis ──produces──▶ Result
```

so "which execution produced this result" is always answerable.

| Node | Backed by artifact? | Notes |
|---|---|---|
| Question / Hypothesis | no | FRAME outputs |
| Source | `research_sources` (see §4) | evidence credibility, verification |
| Dataset / Variable | optional | EVIDENCE/DATA outputs |
| Design | yes (design_register) | design lock target |
| Execution | `research_executions` | every run |
| Analysis / Result | yes | EXECUTE outputs |
| Evidence / Claim | yes (evidence/claim artifacts) | claim governance |
| Table / Figure | yes | WRITE exhibits |
| Paragraph | yes (manuscript) | anchors claims in text |
| Decision | no | design/change decisions |
| Risk | no | risk ledger |
| Gate | `research_gates` | gate check results |

## 3. Edge types (normative set)

`motivates, uses, transformed_by, produces, supports, appears_in, invalidates, overrides,
derived_from, generated_by, depends_on, cites, tests`.

| Edge | Typical tail → head |
|---|---|
| `motivates` | Question → Design / Hypothesis |
| `uses` | Design → Variable; Dataset → Variable |
| `transformed_by` | Dataset → Execution |
| `generated_by` | Execution → Analysis |
| `produces` | Analysis → Result |
| `supports` | Result → Evidence → Claim |
| `appears_in` | Claim / Table / Figure → Paragraph |
| `invalidates` | Risk / Decision → Evidence / Result |
| `overrides` | Decision → Design / Analysis |
| `derived_from` | Artifact → Artifact; Result → Analysis |
| `depends_on` | Analysis → Design; Dataset → Source |
| `cites` | Claim → Source |
| `tests` | Gate → Result / Analysis |

## 4. Source verification (sub-entity of `Source` nodes)

Every Source node carries a `research_sources` record:

```yaml
source_type:          enum(peer_reviewed, working_paper, preprint, official_data,
                          government, institutional_report, news, web_page, blog, user_uploaded)
verification_status:  enum(unverified, existence_verified, metadata_verified,
                          content_verified, citation_verified)
evidence_strength:    enum(high, medium, low)
verified_at:          datetime|null
```

- A search hit is `unverified` until existence is confirmed (URL loads / DOI resolves).
- A citation is only usable as factual support after `content_verified`/`citation_verified`.
- A claim built on an `unverified` source is itself marked `STALE`/`low` until verified.
- Sources are typed so "a web page" and "a peer-reviewed paper" are structurally different.

## 5. Node status: VALID / STALE / INVALID

```text
VALID    consistent with all current upstream evidence
STALE    upstream changed; epistemic uncertainty — needs re-verification, NOT a logic error
INVALID  method refuted or evidence overturned
```

Rules:

1. Editing a `Dataset`/`Design`/`Source` marks every downstream node `STALE` (never
   `INVALID` by default).
2. An `INVALID` requires an explicit reason (a `Decision`/`Risk` node with an
   `invalidates` edge) or a gate FAIL with a recorded cause.
3. A `STALE` node cannot be cited as `VALID` by later stages; the owning stage must
   re-run its checks (gate) to restore `VALID`.

## 6. Cascade invalidation (`invalidate_downstream`)

On a targeted node `n`:

```text
for each downstream node d reachable via supported edges (produces, supports, appears_in,
derived_from, generated_by, depends_on):
    d.status = STALE            # epistemic uncertainty by default
for each node m with an explicit invalidating edge (invalidates) from n, or where n was
    the method that produced m:
    m.status = INVALID          # only with a recorded reason
write an audit row (who, when, node, reason) under the project
```

The caller chooses the reason (e.g. "dataset corrected", "estimator replaced"); the status
transition is mechanical. Restoring `VALID` always requires re-running the relevant gate.

## 7. Query interface (`query_lineage`)

- `query_lineage(node_id, direction=upstream|downstream)` returns the sub-graph within the
  project.
- Impact analysis: given a proposed change to node `n`, return all downstream nodes that
  would become `STALE`/`INVALID` — this is the input to the gate's "change impact" check
  and to the approval request's `risk_assessment`.

## 8. Graph invariants

1. Nodes and edges are Project-scoped; `WHERE project_id = ?` everywhere.
2. `(source_id, target_id, kind)` is unique per project.
3. Every `supports_claims`/`derived_from` on an artifact mirrors a real edge; the graph is
   authoritative.
4. `Execution` nodes are immutable once the execution is terminal.
5. No agent may set a node `VALID` without a gate or verified re-check.
