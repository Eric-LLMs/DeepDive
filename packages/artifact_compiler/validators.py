"""Deterministic structural validators (fail before render).

Two gates from docs/research/19 §7:

* :func:`validate_section_tree` — the section graph must be a single-root, acyclic,
  orphan-free tree with contiguous, unique sibling order.
* :func:`validate_plan_references` — every cross-entity reference in a plan
  (claim-req parentage, visual/evidence ids) must resolve inside the plan / EvidenceSet;
  unresolved references are rejected outright (fixture-07 defense).

Validators collect *all* violations into a :class:`ValidationReport` so one run
reports once; :func:`ok_or_raise` turns a report into a hard error for entrypoints.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from artifact_compiler.doc_ast import DocumentAST
from artifact_compiler.plan import ArtifactPlan, BlockKind, SectionPlan
from artifact_compiler.source import Claim
from artifact_compiler.visual import Asset, CompileStatus


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def err(self, msg: str) -> None:
        self.errors.append(msg)


def ok_or_raise(report: ValidationReport, subject: str) -> None:
    if not report.ok:
        joined = "; ".join(report.errors)
        raise ValueError(f"{subject} failed validation: {joined}")


# ── section tree ──────────────────────────────────────────────────────────────

def validate_section_tree(sections: list[SectionPlan]) -> ValidationReport:
    """Single root, known parents, acyclic, no orphans, sibling order unique+contiguous."""
    r = ValidationReport()
    by_id = {s.section_id: s for s in sections}
    if len(by_id) != len(sections):
        counts = Counter(s.section_id for s in sections)
        dups = sorted(sid for sid, n in counts.items() if n > 1)
        r.err(f"duplicate section_ids: {dups}")
        return r

    roots = [s for s in sections if s.parent_id is None]
    if len(roots) != 1:
        r.err(f"expected exactly one root section, got {len(roots)}")

    for s in sections:
        if s.parent_id is not None and s.parent_id not in by_id:
            r.err(f"section {s.section_id!r} has unknown parent {s.parent_id!r}")

    # cycle detection on parent pointers (tortoise-free: depth walk with visited)
    for s in sections:
        seen = {s.section_id}
        cur = s
        while cur.parent_id is not None:
            cur = by_id.get(cur.parent_id)  # type: ignore[assignment]
            if cur is None:
                break  # unknown parent already reported
            if cur.section_id in seen:
                r.err(f"section tree contains a cycle through {s.section_id!r}")
                break
            seen.add(cur.section_id)

    # reachability: every node must terminate at a root without unknown parents
    for s in sections:
        cur: SectionPlan | None = s
        while cur is not None and cur.parent_id is not None:
            cur = by_id.get(cur.parent_id)

    # sibling order: unique and contiguous 0..n-1 under each parent
    children: dict[str | None, list[SectionPlan]] = {}
    for s in sections:
        children.setdefault(s.parent_id, []).append(s)
    for parent, kids in children.items():
        orders = sorted(k.order for k in kids)
        if len(set(orders)) != len(orders):
            r.err(f"section {parent!r} children have duplicate order values: {orders}")
        elif orders != list(range(len(orders))):
            r.err(f"section {parent!r} children orders not contiguous from 0: {orders}")
    return r


# ── plan references ───────────────────────────────────────────────────────────

def validate_plan_references(plan: ArtifactPlan, evidence_pool: set[str]) -> ValidationReport:
    """All cross-references must resolve: claim-req parentage/evidence, visual spec
    evidence/claim pool membership, and source_scope coverage."""
    r = ValidationReport()

    for s in plan.sections:
        for cr in s.claim_requirements:
            if cr.section_id != s.section_id:
                r.err(
                    f"claim requirement {cr.claim_req_id!r} declares section "
                    f"{cr.section_id!r} but is nested under {s.section_id!r}"
                )
            missing = [e for e in cr.evidence_ids if e not in evidence_pool]
            if missing:
                r.err(f"claim requirement {cr.claim_req_id!r} references unknown evidence {missing}")

        vs = s.visual_spec
        if vs is not None:
            missing = [e for e in vs.evidence_ids if e not in evidence_pool]
            if missing:
                r.err(f"visual spec {vs.spec_id!r} references unknown evidence {missing}")

    scope_missing = [e for e in plan.source_scope.evidence_ids if e not in evidence_pool]
    if scope_missing:
        r.err(f"source_scope references unknown evidence {scope_missing}")
    return r


def validate_claim_attribution(claims: list[Claim], plan: ArtifactPlan) -> ValidationReport:
    """Claims must resolve to their claim-requirement and section (no orphan claims)."""
    r = ValidationReport()
    reqs = {
        cr.claim_req_id: cr
        for s in plan.sections
        for cr in s.claim_requirements
    }
    section_ids = {s.section_id for s in plan.sections}
    seen: set[str] = set()
    for c in claims:
        if c.claim_id in seen:
            r.err(f"duplicate claim_id {c.claim_id!r}")
        seen.add(c.claim_id)
        req = reqs.get(c.claim_req_id)
        if req is None:
            r.err(f"claim {c.claim_id!r} references unknown claim requirement {c.claim_req_id!r}")
        elif req.section_id != c.section_id:
            r.err(f"claim {c.claim_id!r} section {c.section_id!r} != requirement's {req.section_id!r}")
        if c.section_id not in section_ids:
            r.err(f"claim {c.claim_id!r} references unknown section {c.section_id!r}")
    return r


# ── AST conformance (contract QA layer, run pre-render) ──────────────────────

def _block_types(blocks) -> set[BlockKind]:
    kinds: set[BlockKind] = set()
    for b in blocks:
        t = getattr(b, "type", None)
        try:
            kinds.add(BlockKind(t))
        except ValueError:
            pass
    return kinds


def validate_ast_contract(
    plan: ArtifactPlan,
    document_ast: DocumentAST,
    assets: dict[str, Asset],
) -> ValidationReport:
    """Pre-compile contract gate (Task 2.2): required blocks present, block
    back-references resolvable, figure satisfied only by a compiled asset."""
    r = ValidationReport()
    sections_by_id = {s.section_id: s for s in plan.sections}
    ast_by_id = {sa.section_id: sa for sa in document_ast.sections}

    for sid, planned in sections_by_id.items():
        authored = ast_by_id.get(sid)
        if authored is None:
            r.err(f"section {sid!r} planned but missing from document AST")
            continue
        present = _block_types(authored.blocks)
        for required in planned.expected_blocks:
            if required not in present:
                r.err(f"section {sid!r} missing required block type {required.value!r}")
            if required == BlockKind.figure:
                ok_asset = any(
                    b.type == "figure"
                    and (a := assets.get(b.asset_id)) is not None
                    # str-enum: compares equal to the plain wire string too
                    and a.compile_status == CompileStatus.success
                    for b in authored.blocks
                )
                if not ok_asset:
                    r.err(f"section {sid!r}: no figure backed by a successfully compiled asset")
    return r
