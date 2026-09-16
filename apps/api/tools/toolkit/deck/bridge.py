"""TRANSIENT render bridge: PresentationBrief → DeckSpec, pure code, zero LLM.

The M1 Visual Compiler (``deck/compiler/`` + Typst v2, plan §M1.6-7) replaces this
module entirely. Until it lands, the shipped Typst renderer keeps producing the
canonical deck.pdf from a deterministically derived DeckSpec, so the 5-stage
contract and the compat exports (deck.md / deck.pptx) stay intact while
generation has already converged onto the brief.

Objects are built with ``model_construct`` on purpose: the brief has already passed
its own schema + semantic gates (``structured._brief_check``), and the legacy
budgets (title ≤ 12 units, digest facts ≥ 1 …) are LLM-discipline limits for the
retired 4-pass chain, not rendering constraints. Mapping is lossless where the old
vocabulary has a slot for the fact (cards → CARDS items, trace locators →
provenance refs, QUANTITATIVE_CODE → one CHART series); everything else degrades
to the message-only hero, loudly visible in ``visual_plan[].rationale``.
"""
from __future__ import annotations

from . import schema as S
from .models import (
    ContentDigest,
    ContentPayload,
    DeckSpec,
    Fact,
    Item,
    Outline,
    Point,
    ProvenanceRef,
    SectionOutline,
    Series,
    Slide,
    SlideOutlineItem,
    SourceDoc,
    SourceKind,
    VisualPlan,
)


def _prov_from_locator(loc: S.SourceLocator) -> ProvenanceRef:
    lines = None
    if loc.start_line is not None:
        lines = (str(loc.start_line) if loc.end_line in (None, loc.start_line)
                 else f"{loc.start_line}-{loc.end_line}")
    return ProvenanceRef.model_construct(
        source_id=loc.doc_id,
        kind=SourceKind.session if loc.message_id else SourceKind.document,
        message_id=loc.message_id, page=loc.page, lines=lines, quote=loc.source_excerpt,
    )


def _chart_payload(visual: S.VisualSpec, name: str) -> ContentPayload | None:
    """QUANTITATIVE_CODE spec → a single CHART series, or None when unusable."""
    spec = visual.generation_spec or {}
    if (visual.policy is not S.VisualGenerationPolicy.QUANTITATIVE_CODE
            or visual.grammar is not S.VisualGrammar.DATA_CHART):
        return None
    labels, values = spec.get("labels"), spec.get("values")
    if not isinstance(labels, list) or not isinstance(values, list) \
            or len(labels) < 2 or len(labels) != len(values):
        return None
    pts: list[Point] = []
    for x, v in zip(labels[:8], values[:8]):
        try:
            pts.append(Point.model_construct(x=str(x), y=float(v), quant_ref="m0"))
        except (TypeError, ValueError):
            return None
    return ContentPayload.model_construct(
        series=[Series.model_construct(name=name, points=pts)])


def _slide_from_plan(plan: S.SlidePlan, traceability: dict[str, S.TraceabilityNode]
                     ) -> tuple[Slide, VisualPlan]:
    items = [
        Item.model_construct(
            label=c.label,
            detail=c.takeaway + (f" — {c.metric_highlight}" if c.metric_highlight else ""),
        )
        for c in plan.cards
    ]
    payload = _chart_payload(plan.visual_spec, plan.title) or ContentPayload.model_construct(items=items)
    visual_type = "CHART" if payload.series else ("CARDS" if items else "TEXT_HERO")
    refs: list[ProvenanceRef] = []
    for card in plan.cards:
        node = traceability.get(card.trace_id)
        if node and node.locator is not None:
            ref = _prov_from_locator(node.locator)
            if ref not in refs:
                refs.append(ref)
    slide = Slide.model_construct(
        slide_id=f"s{plan.slide_index}",
        title=plan.title,
        key_message=plan.central_message,
        purpose="SUMMARY", relationship="quantitative" if payload.series else "categorical",
        payload=payload, speaker_notes=plan.speaker_notes, provenance_refs=refs,
    )
    rationale = (f"brief bridge: {plan.visual_spec.grammar.value}/"
                 f"{plan.visual_spec.policy.value} → {visual_type}")
    return slide, VisualPlan.model_construct(
        slide_id=slide.slide_id, visual_type=visual_type, rationale=rationale)


def brief_to_deckspec(brief: S.PresentationBrief, *, document_title: str,
                      source_names: list[str],
                      presentation_goal: str = "") -> DeckSpec:
    """Deterministic brief → renderable legacy DeckSpec (see module docstring)."""
    slides, plans = [], []
    for plan in brief.slides:
        slide, vp = _slide_from_plan(plan, brief.traceability_graph)
        slides.append(slide)
        plans.append(vp)
    if slides:
        slides[-1] = slides[-1].model_copy(update={"purpose": "SUMMARY"})

    facts = [
        Fact.model_construct(
            fact_id=f"t{n}", statement=node.statement,
            provenance=([_prov_from_locator(node.locator)] if node.locator else []),
        )
        for n, node in enumerate(brief.traceability_graph.values(), 1)
    ]
    if not facts:
        facts = [Fact.model_construct(fact_id="t0", statement=brief.thesis, provenance=[])]

    names = source_names or [brief.deck_id]
    kind = (SourceKind.session if any(r.kind == SourceKind.session
                                      for s in slides for r in s.provenance_refs)
            else SourceKind.document)
    outline_slides = [
        SlideOutlineItem.model_construct(
            slide_id=s.slide_id, title=s.title, purpose=s.purpose,
            relationship=s.relationship, key_message=s.key_message)
        for s in slides
    ]
    return DeckSpec.model_construct(
        deck_id=brief.deck_id,
        title=document_title or names[0],
        target_audience=brief.target_audience,
        presentation_goal=presentation_goal,
        target_slide_count=brief.target_slide_count,
        narrative_strategy="top_down",
        sources=[SourceDoc.model_construct(source_id=nm, kind=kind, name=nm)
                 for nm in names],
        digest=ContentDigest.model_construct(title=document_title, facts=facts),
        outline=Outline.model_construct(
            title=document_title or names[0], narrative_strategy="top_down",
            sections=[SectionOutline.model_construct(
                title=brief.narrative_arc, purpose="presentation-brief",
                slides=outline_slides)] if outline_slides else [],
        ),
        slides=slides,
        visual_plan=plans,
        section_dividers=False,
        speaker_notes_appendix=False,
    )
