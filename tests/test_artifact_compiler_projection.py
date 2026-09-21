"""Projection invariant tests (docs/research/19 §3 inv.11): the finalized
Research OS manuscript projects to a byte-stable DocumentAST with ZERO content
transformation — determinism, golden snapshot, lossless text, verbatim-quoted
markers, and end-to-end compile off the projection (no LLM in the loop)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from artifact_compiler.doc_ast import ListBlock, ParagraphBlock, TableBlock
from artifact_compiler.plan import ArtifactPlan
from artifact_compiler.projection import (
    ProjectionError,
    project_manuscript_to_ast,
)
from artifact_compiler.source import Citation, Evidence, Locator
from artifact_compiler.typst_compiler import compile_typst, load_default_template

FIX = Path(__file__).parent / "fixtures" / "artifact_compiler"
MANUSCRIPT = (FIX / "manuscript.md").read_text(encoding="utf-8")
SNAP = FIX / "manuscript.ast.snapshot"

_MARKERS = {
    "[src:aa11bb22]": "c1",
    "[src:cc33dd44]": "c2",
    "[src:ee55ff66]": "c3",
}


def _ast() -> object:
    return project_manuscript_to_ast(
        MANUSCRIPT, artifact_id="art-p", citation_markers=_MARKERS,
    )


def _dump(doc) -> str:
    return json.dumps(
        doc.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False,
    )


# ── requirement 2: golden / snapshot determinism ─────────────────────────────

def test_ast_golden_snapshot_byte_exact():
    doc = _ast()
    out = _dump(doc)
    if os.environ.get("ARTIFACT_SNAPSHOT_REGEN") == "1":  # reviewed regeneration only
        SNAP.write_text(out, encoding="utf-8", newline="\n")
    assert out == SNAP.read_text(encoding="utf-8"), "projection drifted from the golden AST"


def test_projection_is_deterministic():
    assert _dump(_ast()) == _dump(_ast())


# ── content preservation: every source token survives, nothing invented ──────

_TOKEN = re.compile(r"[\w%.\-]+", re.UNICODE)


def _all_text(doc) -> str:
    chunks: list[str] = []
    for sec in doc.sections:
        for b in sec.blocks:
            chunks.extend(
                [getattr(b, "text", "")]
                + [i.text for i in getattr(b, "inlines", [])]
                + [it for item in getattr(b, "items", []) for it in
                   (i.text for i in item.inlines)]
                + list(getattr(b, "headers", []) or [])
                + [c for row in getattr(b, "rows", []) or [] for c in row]
            )
    return "\n".join(chunks)


def test_no_token_is_lost():
    doc = _ast()
    text = _all_text(doc)
    # tokens consumed by design: markers (carried as citation ids) and pure
    # markdown syntax (table separators, fence info strings)
    src_tokens = {t for t in _TOKEN.findall(MANUSCRIPT)}
    src_tokens -= {
        "src", "aa11bb22", "cc33dd44", "ee55ff66", "python", "---",
    }
    missing = [t for t in src_tokens if t not in text]
    assert not missing, f"projection dropped tokens: {missing}"


def test_projection_adds_no_prose():
    """The AST text is a subset of the source text (modulo markdown syntax):
    no sentence appears in the AST that is not in the manuscript."""
    doc = _ast()
    joined_src = MANUSCRIPT.replace("\n", "")
    for sec in doc.sections:
        for b in sec.blocks:
            plain = getattr(b, "text", None)
            if (
                plain
                and plain not in joined_src.replace("**", "").replace("`", "")
                and not any(plain in seg for seg in _all_text(doc))
            ):
                pytest.fail(f"synthesized block text {plain!r}")


def test_structure_mapping():
    doc = _ast()
    kinds = [type(b).__name__ for s in doc.sections for b in s.blocks]
    assert "TableBlock" in kinds and "ListBlock" in kinds
    # three level-1 headings => three sections (title + 方法/结果/结论 merge by level)
    assert [s.section_id.startswith("sec-") for s in doc.sections]
    tables = [b for s in doc.sections for b in s.blocks if isinstance(b, TableBlock)]
    assert tables and tables[0].headers == ["组别", "kg/株", "相对增量"]
    lists = [b for s in doc.sections for b in s.blocks if isinstance(b, ListBlock)]
    assert lists and not lists[0].ordered
    paras = [b for s in doc.sections for b in s.blocks if isinstance(b, ParagraphBlock)]
    code = [b for p in paras for b in [p] if p.inlines and p.inlines[0].code]
    assert code, "fenced code must project to a code inline verbatim"


def test_citation_markers_attach_and_remove():
    doc = _ast()
    cites = [
        (i.citations, i.text)
        for s in doc.sections for b in s.blocks
        for i in (getattr(b, "inlines", [])
                  + [x for it in getattr(b, "items", []) for x in it.inlines])
        if i.citations
    ]
    assert sorted(c[0][0] for c in cites) == ["c1", "c2", "c3"]
    flat = _all_text(doc)
    assert "src:" not in flat  # markers consumed only when registered


def test_unregistered_marker_stays_literal():
    doc = project_manuscript_to_ast(MANUSCRIPT, artifact_id="art-p")  # no mapping
    assert "src:aa11bb22" in _all_text(doc)  # never silently dropped


# ── fail-closed edges ─────────────────────────────────────────────────────────

def test_projection_errors():
    with pytest.raises(ProjectionError, match="empty"):
        project_manuscript_to_ast("   ", artifact_id="a")
    with pytest.raises(ProjectionError, match="before the first level-1"):
        project_manuscript_to_ast("orphan body\n", artifact_id="a")


def test_duplicate_h1_titles_mint_unique_section_ids():
    # A real manuscript repeating the same H1 (found in the tomato edition) must
    # not yield duplicate section_ids — the section-tree contract rejects them.
    doc = project_manuscript_to_ast(
        "# T\n\n## A\nbody one\n\n# T\n\nbody two\n", artifact_id="a",
    )
    ids = [s.section_id for s in doc.sections]
    assert len(ids) == len(set(ids)) == 2  # "T" appears twice → second gets -2
    assert _all_text(doc).count("T") >= 2  # titles themselves untouched


# ── end-to-end: projection is the authoritative content source (requirement 3) ─

def test_compiled_pdf_source_comes_from_projection():
    doc = _ast()
    plan = ArtifactPlan(
        artifact_id="art-p",
        metadata={"title": "番茄盆栽产量研究", "authors": ["Delveta"], "keywords": []},
        source_scope={"source_ids": ["s1"], "evidence_ids": ["e1"]},
        summary_spec={"purpose": "p", "core_questions": []},
        sections=[{
            "section_id": s.section_id, "parent_id": None, "order": i,
            "title": "t", "content_mode": "synthesis",
        } for i, s in enumerate(doc.sections)],
    )
    # single-root rule only applies to SectionPlan trees; the AST is flat by design
    ev = {"e1": Evidence(evidence_id="e1", source_id="s1", locator=Locator(page=1),
                         excerpt="x", evidence_type="fact")}
    cits = {cid: Citation(citation_id=cid, evidence_ids=["e1"])
            for cid in ("c1", "c2", "c3")}
    out = compile_typst(
        plan, doc, template=load_default_template(),
        citations=cits, evidence_by_id=ev, source_names={"s1": "S"},
    )
    for needle in ("= 番茄盆栽产量研究", "滴灌 + 全光谱 #cnum[1]", "Treated A",
                   "#cnum[2]", "#cnum[3]", "yield_gain = (2.9 - 2.1) / 2.1"):
        assert needle in out
    assert out.count("#refEntry(") == 3
