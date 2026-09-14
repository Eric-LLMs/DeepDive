"""Shared fixture builders for deck-engine tests (models / rules / layout / passes).

Pure factories — no I/O, no LLM. Text is mixed CJK + Latin on purpose so the
``text_units`` budget and the measurement path are exercised everywhere.
"""
from __future__ import annotations

from apps.api.tools.toolkit.deck.models import (
    Column,
    ContentDigest,
    ContentPayload,
    DeckSpec,
    Fact,
    Item,
    Outline,
    Point,
    ProvenanceRef,
    Quantitative,
    SectionOutline,
    Series,
    Slide,
    SlideOutlineItem,
    SourceDoc,
    Step,
)


def ref(lines: str = "1-3", **kw) -> ProvenanceRef:
    return ProvenanceRef(source_id="src1", kind="document", lines=lines, **kw)


def make_digest(with_quantities: bool = True) -> ContentDigest:
    facts = [
        Fact(fact_id="f1", statement="RAG 结合检索与生成", provenance=[ref("1-5")]),
        Fact(fact_id="f2", statement="向量库按谓词隔离", provenance=[ref("10-12")]),
        Fact(fact_id="f3", statement="成本主要来自 re-verify",
             provenance=[ref("20-24")]),
    ]
    quantities = []
    if with_quantities:
        quantities = [
            Quantitative(quant_id="q1", metric="EVIDENCE 耗时", value=72.0, unit="%",
                         provenance=[ref("30")]),
            Quantitative(quant_id="q2", metric="成本", value=0.565, unit="USD",
                         provenance=[ref("31")]),
        ]
    return ContentDigest(title="检索增强", facts=facts,
                         concepts=["RAG", "隔离"], quantities=quantities)


def make_outline(n_slides: int = 3, last_summary: bool = True) -> Outline:
    slides = [
        SlideOutlineItem(slide_id=f"s{i+1}", title=f"第 {i+1} 页",
                         purpose=p, relationship=r, key_message=f"key message {i+1}",
                         fact_refs=[f"f{i % 3 + 1}"])
        for i, (p, r) in enumerate([
            ("PROBLEM", "singular_takeaway"),
            ("PROCESS", "sequential"),
            ("SUMMARY", "singular_takeaway") if last_summary else ("COMPARISON", "comparative"),
        ][:n_slides])
    ]
    return Outline(title="测试 Deck", narrative_strategy="problem_solution",
                   sections=[SectionOutline(title="主体", purpose="main", slides=slides)])


def slide_payload(**kw) -> ContentPayload:
    """Shape slots only — the slide's message lives on Slide.key_message."""
    return ContentPayload(**kw)


def make_slide(slide_id: str = "s1", **kw) -> Slide:
    kw.setdefault("title", "单页标题")
    kw.setdefault("key_message", "这一页只讲一个结论")
    kw.setdefault("purpose", "PROCESS")
    kw.setdefault("relationship", "sequential")
    kw.setdefault("payload", slide_payload())
    kw.setdefault("speaker_notes", "notes")
    return Slide(slide_id=slide_id, provenance_refs=[ref()], **kw)


def hero_slide() -> Slide:
    return make_slide(purpose="SUMMARY", relationship="singular_takeaway",
                      key_message="少做 re-verify,多留证据复用")


def cards_slide(n: int = 3) -> Slide:
    return make_slide(relationship="categorical", payload=slide_payload(
        items=[Item(label=f"概念{i}", detail=f"说明 {i} explains itself")
               for i in range(1, n + 1)]))


def flow_slide(n: int = 4, with_when: bool = False) -> Slide:
    return make_slide(payload=slide_payload(
        steps=[Step(label=f"步骤{i}", detail=f"step {i} does work",
                    when=f"202{i}-01" if with_when else "")
               for i in range(1, n + 1)]))


def arch_slide() -> Slide:
    return make_slide(relationship="hierarchical", payload=slide_payload(
        items=[Item(label="API", group="接入层"), Item(label="Worker", group="执行层"),
               Item(label="Store", group="存储层"), Item(label="Drive", group="存储层")]))


def compare_slide() -> Slide:
    return make_slide(relationship="comparative", payload=slide_payload(
        columns=[Column(header="宽松", cells=["快", "省", "默认"]),
                 Column(header="严格", cells=["稳", "全", "审计"])],
    ))


def chart_slide(fabricated: bool = False) -> Slide:
    points = [
        Point(x="2024", y=72.0, quant_ref="q_missing" if fabricated else "q1"),
        Point(x="2025", y=0.565, quant_ref="q_missing" if fabricated else "q2"),
    ]
    return make_slide(purpose="DATA_INSIGHT", relationship="quantitative",
                      payload=slide_payload(series=[Series(name="指标", points=points)]))


def make_deck(slides: list[Slide] | None = None) -> DeckSpec:
    from apps.api.tools.toolkit.deck.rules import derive_visual_plan
    digest = make_digest()
    base = slides or [hero_slide(), cards_slide(), flow_slide()]
    # deterministic unique ids keep slide ↔ outline ↔ plan alignment
    slides = [s.model_copy(update={"slide_id": f"s{i+1}"}) for i, s in enumerate(base)]
    outline = Outline(
        title="测试 Deck", narrative_strategy="problem_solution",
        sections=[SectionOutline(
            title="主体", purpose="main",
            slides=[SlideOutlineItem(
                slide_id=s.slide_id, title=s.title, purpose=s.purpose,
                relationship=s.relationship, key_message=s.key_message,
                fact_refs=["f1"]) for s in slides]),
    ])
    return DeckSpec(
        deck_id="d1", title="测试 Deck", target_slide_count=len(slides),
        narrative_strategy="problem_solution",
        sources=[SourceDoc(source_id="src1", kind="document", name="doc.md")],
        digest=digest, outline=outline, slides=slides,
        visual_plan=[derive_visual_plan(s, digest) for s in slides],
    )
