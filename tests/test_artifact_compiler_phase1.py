"""Phase-1 unit tests: sanitizer, density gate, grounding reducer, verdict,
patch application, render guards, and the Core zero-LLM purity guard."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from artifact_compiler.doc_ast import (
    DocumentAST,
    FigureBlock,
    HeadingBlock,
    ParagraphBlock,
    SectionAST,
)
from artifact_compiler.plan import VisualSpec
from artifact_compiler.qa import (
    GroundingDiagnosis,
    decide_verdict,
    reduce_grounding,
)
from artifact_compiler.repair import (
    PatchError,
    ReplaceAssetPatch,
    ReplaceBlockPatch,
    apply_patch,
)
from artifact_compiler.source import Claim, GroundingStatus
from artifact_compiler.typst_compiler import run_typst_compile
from artifact_compiler.validators import ValidationReport
from artifact_compiler.visual_engine import (
    MermaidConfig,
    RenderInputError,
    density_issues,
    is_svg_clean,
    render_mermaid,
    sanitize_svg,
)
from pydantic import ValidationError

# ── SVG sanitization ──────────────────────────────────────────────────────────

def test_sanitize_removes_script_event_and_external():
    dirty = (
        '<svg xmlns="http://www.w3.org/2000/svg" id="s">'
        "<script>alert(1)</script>"
        '<script href="x/y.js"/>'
        '<rect onclick="bad()" fill="red"/>'
        '<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">hi</div></foreignObject>'
        '<a xlink:href="https://evil.example/x"><text>l</text></a>'
        '<image href="http://evil/x.png"/>'
        "<style>@import url('http://evil/x.css');</style>"
        '<use href="#ok"/>'
        "</svg>"
    )
    clean = sanitize_svg(dirty)
    assert "<script" not in clean
    assert "onclick" not in clean and 'fill="red"' in clean
    assert "foreignObject" not in clean
    assert "evil" not in clean
    assert '@import' not in clean
    assert '<use href="#ok"/>' in clean          # in-document refs survive
    assert is_svg_clean(clean)
    assert sanitize_svg(clean) == clean          # idempotent / fixpoint


def test_sanitize_keeps_data_uri():
    svg = '<image href="data:image/png;base64,iVBORw0KGgo="/>'
    assert sanitize_svg(svg) == svg


# ── density gate ──────────────────────────────────────────────────────────────

def _spec(entities, rels=None, max_nodes=12):
    return VisualSpec(
        spec_id="v", visual_format="flowchart", purpose="p",
        entities=entities,
        relationships=[{"from": a, "to": b} for a, b in (rels or [])],
        claim_ids=["c"], evidence_ids=["e"],
        constraints={"max_nodes": max_nodes},
    )


def test_density_clean_spec_passes():
    s = _spec(["A", "B", "C"], [("A", "B"), ("B", "C")])
    assert density_issues(s) == []


def test_density_flags_overload():
    s = _spec([f"N{i}" for i in range(13)], [("N0", "N1")])
    assert any("max_nodes" in i for i in density_issues(s))
    s = _spec(["A", "B"], [("A", "B")] * 6)
    assert any("hairball" in i for i in density_issues(s))
    s = _spec(["A" * 40], [])
    assert any("28 chars" in i for i in density_issues(s))


# ── grounding reducer (authoritative write boundary) ─────────────────────────

def _claims(*ids: str) -> list[Claim]:
    return [
        Claim(claim_id=i, claim_req_id="cr", section_id="s", text="t",
              evidence_ids=["e1"])
        for i in (ids or ("c1", "c2"))
    ]


def _diag(verdicts, threshold=0.8):
    return GroundingDiagnosis(
        qa_run_id="q1", judge_model="judge-x", judge_prompt_version="v2",
        threshold=threshold, verdicts=verdicts,
    )


def test_reduce_writes_authoritative_fields_and_record():
    out = reduce_grounding(_claims("c1", "c2"), _diag([
        {"claim_id": "c1", "grounding_status": "supported", "entailment_score": 0.95},
        {"claim_id": "c2", "grounding_status": "partially_supported",
         "entailment_score": 0.6, "rationale": "r"},
    ]))
    assert out.ok
    by = {c.claim_id: c for c in out.claims}
    assert by["c1"].grounding_status == GroundingStatus.supported
    assert by["c2"].grounding_status == GroundingStatus.partially_supported
    assert not out.all_supported
    assert out.flagged_claim_ids == ["c2"]
    rec = out.record
    assert rec["judge_model"] == "judge-x"
    assert rec["by_status"] == {"partially_supported": 1, "supported": 1}


def test_reduce_rejects_incomplete_or_inconsistent():
    # missing verdict for c2
    out = reduce_grounding(_claims(), _diag(
        [{"claim_id": "c1", "grounding_status": "supported", "entailment_score": 0.9}]
    ))
    assert not out.ok and any("without verdict" in e for e in out.errors)
    # pending not admissible
    out = reduce_grounding(_claims(), _diag([
        {"claim_id": "c1", "grounding_status": "pending"},
        {"claim_id": "c2", "grounding_status": "supported", "entailment_score": 0.9},
    ]))
    assert not out.ok and any("pending" in e for e in out.errors)
    # unknown claim verdict
    out = reduce_grounding(_claims(), _diag([
        {"claim_id": "ghost", "grounding_status": "supported"},
        {"claim_id": "c1", "grounding_status": "supported"},
        {"claim_id": "c2", "grounding_status": "supported"},
    ]))
    assert not out.ok
    # score below threshold but status supported — contradictory
    out = reduce_grounding(_claims(), _diag([
        {"claim_id": "c1", "grounding_status": "supported", "entailment_score": 0.4},
        {"claim_id": "c2", "grounding_status": "supported"},
    ]))
    assert not out.ok and any("threshold" in e for e in out.errors)
    # nothing partially applied: input claims untouched
    src = _claims()
    reduce_grounding(src, _diag([{"claim_id": "c1", "grounding_status": "supported"}]))
    assert all(c.grounding_status == GroundingStatus.pending for c in src)


def test_diagnosis_schema_no_extra_fields():
    with pytest.raises(ValidationError):
        GroundingDiagnosis(
            qa_run_id="q", judge_model="m", judge_prompt_version="v",
            verdicts=[{"claim_id": "c"}], writer_override=True,
        )


# ── verdict reduction ─────────────────────────────────────────────────────────

def _good_grounding(all_supported=True):
    out = reduce_grounding(_claims("c1"), _diag(
        [{"claim_id": "c1", "grounding_status": "supported" if all_supported
          else "unsupported"}]
    ))
    return out


def test_decide_verdict_priority():
    ok_contract = ValidationReport()
    g_pass, g_flag = _good_grounding(True), _good_grounding(False)
    assert decide_verdict(contract=ok_contract, grounding=g_pass) == "pass"
    assert decide_verdict(contract=ok_contract, grounding=g_flag) == "needs_review"
    bad = ValidationReport(errors=["x"])
    assert decide_verdict(contract=bad, grounding=g_pass) == "repair"
    assert decide_verdict(contract=bad, grounding=g_pass, repair_attempts=3) == "blocked"
    assert decide_verdict(contract=ok_contract, grounding=g_pass,
                          rendered_issues=["overflow"]) == "repair"
    assert decide_verdict(contract=ok_contract, grounding=None) == "repair"
    # grounding gaps never trigger repair — production policy is NEEDS_REVIEW
    assert decide_verdict(contract=ok_contract, grounding=g_flag,
                          repair_attempts=0) == "needs_review"


# ── patch application ─────────────────────────────────────────────────────────

def _doc() -> DocumentAST:
    return DocumentAST(artifact_id="a", sections=[SectionAST(section_id="s", blocks=[
        HeadingBlock(block_id="h", level=1, text="T"),
        ParagraphBlock(block_id="p", inlines=[{"text": "old"}]),
        FigureBlock(block_id="f", asset_id="a1",
                    svg_relative_path="assets/a1.svg", caption="c"),
    ])])


def test_replace_block_same_id_and_type():
    doc = _doc()
    patch = ReplaceBlockPatch(
        block_id="p", reason="grammar",
        block=ParagraphBlock(block_id="p", inlines=[{"text": "new"}]),
    )
    out = apply_patch(doc, patch)
    assert out.sections[0].blocks[1].inlines[0].text == "new"
    # original untouched (pure transformation)
    assert doc.sections[0].blocks[1].inlines[0].text == "old"
    assert out is not doc


def test_patch_rejections():
    doc = _doc()
    with pytest.raises(PatchError, match="no block"):
        apply_patch(doc, ReplaceBlockPatch(
            block_id="ghost",
            block=ParagraphBlock(block_id="ghost", inlines=[{"text": "x"}]),
        ))
    with pytest.raises(PatchError, match="block ids are the repair address"):
        apply_patch(doc, ReplaceBlockPatch(
            block_id="p", block=ParagraphBlock(block_id="other", inlines=[{"text": "x"}]),
        ))
    with pytest.raises(PatchError, match="changes block type"):
        apply_patch(doc, ReplaceBlockPatch(
            block_id="p",
            block=HeadingBlock(block_id="p", level=2, text="sneak"),
        ))
    with pytest.raises(PatchError, match="not a figure"):
        apply_patch(doc, ReplaceAssetPatch(
            block_id="p", asset_id="a2", svg_relative_path="assets/a2.svg",
        ))


def test_replace_asset_repoints_figure():
    out = apply_patch(_doc(), ReplaceAssetPatch(
        block_id="f", asset_id="a2", svg_relative_path="assets/a2.svg",
    ))
    fig = out.sections[0].blocks[2]
    assert isinstance(fig, FigureBlock)
    assert fig.asset_id == "a2" and fig.svg_relative_path == "assets/a2.svg"
    assert fig.caption == "c"  # untouched fields preserved


# ── render guards (no mmdc binary needed for these paths) ────────────────────

@pytest.mark.asyncio
async def test_render_guards_before_exec(tmp_path):
    with pytest.raises(RenderInputError, match="empty"):
        await render_mermaid("  ", tmp_path / "a.mmd", tmp_path / "a.svg")
    with pytest.raises(RenderInputError, match="input cap"):
        await render_mermaid("flowchart TD\n" + ("x" * 70_000),
                             tmp_path / "a.mmd", tmp_path / "a.svg")
    with pytest.raises(RenderInputError, match="not found"):
        await render_mermaid(
            "flowchart TD\nA-->B", tmp_path / "a.mmd", tmp_path / "a.svg",
            cfg=MermaidConfig(mmdc_bin="definitely-not-installed-mmdc"),
        )


@pytest.mark.asyncio
async def test_render_happy_path_if_mmdc_present(tmp_path):
    import shutil
    if shutil.which("mmdc") is None:
        pytest.skip("mmdc not installed locally — environment prerequisite")
    res = await render_mermaid(
        "flowchart TD\n  A[Start] --> B[Done]",
        tmp_path / "g.mmd", tmp_path / "g.svg",
    )
    assert res.ok, res.stderr
    assert is_svg_clean(res.svg_path.read_text(encoding="utf-8"))


def test_run_typst_compile_missing_binary_is_failure_not_raise(tmp_path):
    ok, err = run_typst_compile(
        "= Hi\n", tmp_path, tmp_path / "out.pdf",
        typst_bin="definitely-not-installed-typst",
    )
    assert ok is False and err
    assert (tmp_path / "report.typ").read_text(encoding="utf-8") == "= Hi\n"


# ── Core purity guard (docs/research/19 §11 DoD; mirrors test_workflow_purity) ─

ALLOWED_STDLIB_OK = {
    "__future__", "asyncio", "contextlib", "dataclasses", "enum", "hashlib",
    "json", "os", "pathlib", "platform", "re", "resource", "shutil", "signal",
    "subprocess", "tempfile", "time", "typing", "collections", "datetime",
    "uuid", "ast",
}
THIRD_PARTY_OK = {"pydantic", "portalocker"}
FORBIDDEN = {
    "openai", "httpx", "requests", "litellm", "socket", "urllib",
    "agent", "core", "plugins", "apps", "rag", "workflow", "shared",
}


def test_core_package_has_no_llm_or_network_or_app_imports():
    pkg = Path(__file__).resolve().parents[1] / "packages" / "artifact_compiler"
    offenders: list[str] = []
    for py in sorted(pkg.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for root in names:
                if root in FORBIDDEN:
                    offenders.append(f"{py.name}: imports {root!r}")
                elif (
                    root not in ALLOWED_STDLIB_OK
                    and root not in THIRD_PARTY_OK
                    and root != "artifact_compiler"
                ):
                    offenders.append(f"{py.name}: unexpected import {root!r}")
    assert not offenders, "\n".join(offenders)
