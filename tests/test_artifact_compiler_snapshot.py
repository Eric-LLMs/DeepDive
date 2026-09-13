"""Phase-1 golden-snapshot test for the Typst compiler (docs/research/19 §11 DoD-1):
the same AST must compile to byte-identical ``.typ`` source every time.

``build_inputs`` is the shared fixture; ``snapshot_report_typ`` regenerates only when
the environment variable ``ARTIFACT_SNAPSHOT_REGEN=1`` is set, so the committed
snapshot is reviewed on purpose, never silently rewritten.
"""
from __future__ import annotations

import os
from pathlib import Path

from artifact_compiler.doc_ast import (
    CalloutBlock,
    DocumentAST,
    FigureBlock,
    HeadingBlock,
    ListBlock,
    ListItem,
    ParagraphBlock,
    SectionAST,
    TableBlock,
)
from artifact_compiler.plan import ArtifactPlan
from artifact_compiler.source import Citation, Evidence, Locator
from artifact_compiler.typst_compiler import compile_typst, load_default_template

SNAPSHOT = (
    Path(__file__).parent / "fixtures" / "artifact_compiler" / "report.typ.snapshot"
)


def build_inputs():
    plan = ArtifactPlan(
        artifact_id="art-1",
        metadata={
            "title": "Dive Report: 番茄盆栽 [v1]",
            "authors": ["Enhui", "DeepDive"],
            "keywords": ["tomato", "研究"],
            "target_audience": "growers",
        },
        source_scope={"source_ids": ["s1", "s2"], "evidence_ids": ["e1", "e2"]},
        summary_spec={"purpose": "p", "core_questions": []},
        sections=[
            {
                "section_id": "root", "parent_id": None, "order": 0,
                "title": "Overview", "content_mode": "explanation",
            },
            {
                "section_id": "s2", "parent_id": "root", "order": 1,
                "title": "Findings", "content_mode": "analysis",
            },
        ],
    )
    doc = DocumentAST(
        artifact_id="art-1",
        sections=[
            SectionAST(section_id="root", blocks=[
                HeadingBlock(block_id="h1", level=1, text="Overview"),
                ParagraphBlock(block_id="p1", inlines=[
                    {"text": "Tomato yields rose "},
                    {"text": "38%", "bold": True, "citations": ["c1"]},
                    {"text": " in 2025 $ trials — see "},
                    {"text": "grow_log.md", "code": True, "citations": ["c2"]},
                ], claim_ids=["cl1"]),
                ListBlock(block_id="l1", ordered=False, items=[
                    ListItem(inlines=[{"text": "drip irrigation"}]),
                    ListItem(inlines=[{"text": "12h light cycle", "citations": ["c1"]}]),
                ]),
            ]),
            SectionAST(section_id="s2", blocks=[
                HeadingBlock(block_id="h2", level=1, text="Findings"),
                TableBlock(block_id="t1", headers=["Variant", "kg/plant"],
                           rows=[["Control", "2.1"], ["Treated", "2.9"]],
                           caption="Yield table"),
                FigureBlock(block_id="f1", asset_id="a1",
                            svg_relative_path="assets/a1.svg",
                            caption="Pipeline — step 3"),
                CalloutBlock(block_id="co1", variant="key_finding",
                             title="Key finding #1",
                             body=[ParagraphBlock(block_id="cp1", inlines=[
                                 {"text": "The treated group outperformed at p<0.01"},
                             ])]),
            ]),
        ],
    )
    evidence = {
        "e1": Evidence(evidence_id="e1", source_id="s1", locator=Locator(page=7),
                       excerpt="x", evidence_type="statistic"),
        "e2": Evidence(evidence_id="e2", source_id="s2",
                       locator=Locator(url="https://example.org/log"),
                       excerpt="y", evidence_type="fact"),
    }
    citations = {
        "c1": Citation(citation_id="c1", evidence_ids=["e1"]),
        "c2": Citation(citation_id="c2", evidence_ids=["e2", "e1"]),
    }
    names = {"s1": "Field Trial 2025", "s2": "Grow Log"}
    return plan, doc, evidence, citations, names


def _compile() -> str:
    plan, doc, evidence, citations, names = build_inputs()
    return compile_typst(
        plan, doc,
        template=load_default_template(),
        citations=citations, evidence_by_id=evidence, source_names=names,
    )


def test_golden_snapshot_byte_exact():
    out = _compile()
    if os.environ.get("ARTIFACT_SNAPSHOT_REGEN") == "1":  # manual regeneration only
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(out, encoding="utf-8", newline="\n")
    expected = SNAPSHOT.read_text(encoding="utf-8")
    assert out == expected, "compiled .typ drifted from the golden snapshot"


def test_compile_is_deterministic():
    assert _compile() == _compile()
