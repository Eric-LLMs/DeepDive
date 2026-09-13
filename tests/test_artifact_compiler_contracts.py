"""Phase-0 contract & validator tests for the Research Artifact Compiler
(docs/research/19 §7, §11). Includes the fixture-07-style negative defenses:
over-privileged writer payloads, hollow topologies and dangling references must be
rejected by the schema/validators themselves, not by convention."""
from __future__ import annotations

import pytest
from artifact_compiler.doc_ast import (
    CalloutBlock,
    DocumentAST,
    FigureBlock,
    ParagraphBlock,
    SectionAST,
    TableBlock,
)
from artifact_compiler.plan import ArtifactPlan, SectionPlan, VisualSpec
from artifact_compiler.source import (
    Citation,
    Claim,
    ClaimRequirement,
    Evidence,
    EvidenceConflict,
    Locator,
    WriterClaimOutput,
)
from artifact_compiler.validators import (
    validate_ast_contract,
    validate_claim_attribution,
    validate_plan_references,
    validate_section_tree,
)
from artifact_compiler.visual import Asset
from pydantic import ValidationError

# ── helpers ───────────────────────────────────────────────────────────────────

def ev(eid: str = "e1") -> Evidence:
    return Evidence(
        evidence_id=eid, source_id="s1", locator=Locator(page=3),
        excerpt="x", evidence_type="fact",
    )


def claim_req(rid: str = "cr1", sid: str = "root", eids: list[str] | None = None) -> ClaimRequirement:
    return ClaimRequirement(
        claim_req_id=rid, section_id=sid, purpose="p", evidence_ids=eids or ["e1"]
    )


def section(sid: str, parent: str | None = None, order: int = 0, **kw) -> SectionPlan:
    return SectionPlan(
        section_id=sid, parent_id=parent, order=order, title=sid,
        content_mode="explanation", **kw,
    )


def plan_with(sections: list[SectionPlan], eids: list[str] | None = None) -> ArtifactPlan:
    return ArtifactPlan(
        artifact_id="a1",
        metadata={"title": "t", "authors": ["me"], "keywords": []},
        source_scope={"source_ids": ["s1"], "evidence_ids": eids or ["e1"]},
        summary_spec={"purpose": "p", "core_questions": []},
        sections=sections,
    )


# ── source contracts ──────────────────────────────────────────────────────────

def test_locator_requires_anchor():
    Locator(page=1)
    Locator(chunk_id="c")
    with pytest.raises(ValidationError, match="at least one"):
        Locator()


def test_evidence_and_citation_min_cardinality():
    with pytest.raises(ValidationError):
        Evidence(evidence_id="e", source_id="s", locator=Locator(page=1),
                 excerpt="", evidence_type="fact")
    with pytest.raises(ValidationError):
        Citation(citation_id="c1", evidence_ids=[])


def test_claim_requirement_needs_evidence():
    with pytest.raises(ValidationError):
        ClaimRequirement(claim_req_id="r", section_id="s", purpose="p", evidence_ids=[])


def test_writer_claim_forbids_grounding_fields():
    """The write-authority split is a TYPE constraint (invariant 3)."""
    WriterClaimOutput(claim_id="c", claim_req_id="cr", section_id="s",
                      text="t", evidence_ids=["e1"])
    with pytest.raises(ValidationError):
        WriterClaimOutput(
            claim_id="c", claim_req_id="cr", section_id="s", text="t",
            evidence_ids=["e1"], grounding_status="supported",  # forbidden on writer shape
        )
    # claim_req_id is required — no orphan claims
    with pytest.raises(ValidationError):
        WriterClaimOutput(claim_id="c", section_id="s", text="t", evidence_ids=["e1"])


def test_claim_grounding_default_and_range():
    c = Claim(claim_id="c", claim_req_id="cr", section_id="s", text="t", evidence_ids=["e1"])
    assert c.grounding_status == "pending"
    assert c.entailment_score is None
    with pytest.raises(ValidationError):
        Claim(claim_id="c", claim_req_id="cr", section_id="s", text="t",
              evidence_ids=["e1"], entailment_score=1.5)


def test_conflict_needs_two_evidence():
    with pytest.raises(ValidationError):
        EvidenceConflict(conflict_id="k", claim_ids=["c"], evidence_ids=["e1"],
                         type="numeric", resolution="unresolved", resolution_reason="r")
    EvidenceConflict(conflict_id="k", claim_ids=["c"], evidence_ids=["e1", "e2"],
                     type="numeric", resolution="present-both", resolution_reason="r")


# ── plan contracts ────────────────────────────────────────────────────────────

def test_visual_spec_strong_traceability():
    base = {"spec_id": "v1", "visual_format": "flowchart", "purpose": "p",
            "entities": ["A", "B"], "relationships": []}
    with pytest.raises(ValidationError):
        VisualSpec(**base, claim_ids=[], evidence_ids=["e1"])
    with pytest.raises(ValidationError):
        VisualSpec(**base, claim_ids=["c1"], evidence_ids=[])
    spec = VisualSpec(**base, claim_ids=["c1"], evidence_ids=["e1"])
    assert spec.constraints.max_nodes == 12
    # alias handling: "from"/"to" wire names
    from artifact_compiler.plan import VisualRelationship
    rel = VisualRelationship.model_validate({"from": "A", "to": "B", "label": "x"})
    assert rel.from_id == "A" and rel.to_id == "B"


def test_section_order_nonnegative():
    with pytest.raises(ValidationError):
        section("s", order=-1)


# ── section tree validator ───────────────────────────────────────────────────

def test_section_tree_happy():
    r = validate_section_tree([
        section("root"),
        section("a", parent="root", order=0),
        section("b", parent="root", order=1),
    ])
    assert r.ok, r.errors


def test_section_tree_rejects_two_roots():
    r = validate_section_tree([section("r1"), section("r2")])
    assert not r.ok and "root" in r.errors[0]


def test_section_tree_rejects_orphan_and_cycle_and_orders():
    r = validate_section_tree([section("root"), section("x", parent="missing")])
    assert not r.ok
    # sibling order gap
    r = validate_section_tree([
        section("root"), section("a", parent="root", order=0),
        section("b", parent="root", order=2),
    ])
    assert not r.ok and "contiguous" in r.errors[0]
    # duplicate sibling order
    r = validate_section_tree([
        section("root"), section("a", parent="root", order=0),
        section("b", parent="root", order=0),
    ])
    assert not r.ok and "duplicate order" in r.errors[0]
    # duplicate section ids
    r = validate_section_tree([section("root"), section("root")])
    assert not r.ok and "duplicate section_ids" in r.errors[0]


# ── plan reference validator ─────────────────────────────────────────────────

def test_plan_references_happy():
    p = plan_with([section("root", claim_requirements=[claim_req()])])
    assert validate_plan_references(p, {"e1"}).ok


def test_plan_references_reject_dangling():
    # claim-req evidence not in pool
    p = plan_with([section("root", claim_requirements=[claim_req(eids=["ghost"])])])
    r = validate_plan_references(p, {"e1"})
    assert not r.ok and "unknown evidence" in r.errors[0]
    # claim-req section mismatch (nested under root but declares other)
    p = plan_with([section("root", claim_requirements=[claim_req(sid="elsewhere")])])
    assert not validate_plan_references(p, {"e1"}).ok
    # source_scope evidence outside pool
    p = plan_with([section("root")], eids=["e1", "e2"])
    assert not validate_plan_references(p, {"e1"}).ok


def test_claim_attribution():
    p = plan_with([section("root", claim_requirements=[claim_req()])])
    good = Claim(claim_id="c1", claim_req_id="cr1", section_id="root",
                 text="t", evidence_ids=["e1"])
    assert validate_claim_attribution([good], p).ok
    orphan = Claim(claim_id="c2", claim_req_id="ghost", section_id="root",
                   text="t", evidence_ids=["e1"])
    assert not validate_claim_attribution([orphan], p).ok


# ── AST + contract validator ─────────────────────────────────────────────────

def test_callout_nesting_and_extra_forbid():
    CalloutBlock(
        block_id="b", variant="key_finding", title="t",
        body=[ParagraphBlock(block_id="p", inlines=[{"text": "hi"}]),
              TableBlock(block_id="t", headers=["h"], rows=[["v"]])],
    )
    with pytest.raises(ValidationError):
        # a Writer cannot smuggle grounding state onto a block either
        ParagraphBlock(block_id="p", inlines=[{"text": "hi"}], grounding_status="supported")


def test_ast_contract_requires_expected_blocks_and_compiled_figure():
    spec = VisualSpec(spec_id="v1", visual_format="flowchart", purpose="p",
                      entities=["A"], claim_ids=["c1"], evidence_ids=["e1"])
    planned = section("root", expected_blocks=["paragraph", "figure"],
                      visual_spec=spec)
    p = plan_with([planned])
    doc = DocumentAST(artifact_id="a1", sections=[SectionAST(
        section_id="root",
        blocks=[ParagraphBlock(block_id="p", inlines=[{"text": "x"}])],
    )])
    r = validate_ast_contract(p, doc, assets={})
    assert not r.ok and any("figure" in e for e in r.errors)
    failed_asset = Asset(asset_id="a1", spec_id="v1", renderer="mermaid",
                         source_path="assets/a1.mmd", output_path="assets/a1.svg",
                         compile_status="failed", claim_ids=["c1"], evidence_ids=["e1"])
    doc2 = DocumentAST(artifact_id="a1", sections=[SectionAST(
        section_id="root",
        blocks=list(doc.sections[0].blocks) + [FigureBlock(
            block_id="f", asset_id="a1", svg_relative_path="assets/a1.svg",
            caption="c")],
    )])
    r = validate_ast_contract(p, doc2, assets={"a1": failed_asset})
    assert not r.ok and any("successfully compiled" in e for e in r.errors)
    ok_asset = failed_asset.model_copy(update={"compile_status": "success"})
    assert validate_ast_contract(p, doc2, assets={"a1": ok_asset}).ok
