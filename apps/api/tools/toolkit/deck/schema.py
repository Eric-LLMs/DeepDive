"""Grounded Visual Presentation Engine: canonical data contracts.

Two layers live here:

1. **Input representation** (``DocumentRepresentation`` and friends) — the
   multimodal output contract of :mod:`.ingest` (faithful physical extraction:
   text blocks with stable locators + sliced visual assets, zero LLM inference).
2. **Cognitive / planning IR** (``SectionUnderstanding`` → ``GlobalMentalModel``
   → ``PresentationBrief``) — the single canonical intermediate representation
   of a deck. ``brief.json`` is the truth carrier; ``.pptx`` / ``.pdf`` /
   ``.md`` and materialized images are derived artifacts only.

Repo conventions kept from the previous engine:
- ``extra="forbid"`` everywhere: an invented field is a validation error that
  feeds the corrective-retry path (never silently ignored).
- one mixed-script yardstick (:func:`text_units`) for every word budget.
- closed enumerations mirrored verbatim in prompts; drift is pinned by tests.

Provenance discipline (§9.2): ``start_line``/``end_line`` may only be filled
from the deterministic extracted-text line mapping; ``page`` + ``bbox`` +
``source_excerpt`` are the primary physical anchor. A locator is only usable
when it carries at least one physical coordinate.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── shared budget yardstick (single source of truth) ──────────────────────────

_CJK_RE = re.compile(r"[⺀-〱㐀-䶿一-鿿豈-﫿]")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-.]*")


def text_units(text: str) -> int:
    """One unit per CJK char, one per Latin/number word.

    ``"检索增强 生成 RAG pipeline"`` → 4 CJK + 2 Latin = 6 units. Every budget
    (card takeaway, slide title, key message) is expressed against this.
    """
    cjk = len(_CJK_RE.findall(text or ""))
    latin = len(_WORD_RE.findall(_CJK_RE.sub(" ", text or "")))
    return cjk + latin


def _forbid(**kwargs) -> ConfigDict:
    return ConfigDict(extra="forbid", **kwargs)


# budgets (prompt-mirrored; keep deck/prompts.py enum/budget tables in sync)
TITLE_MAX = 14                 # slide titles
CENTRAL_MESSAGE_MAX = 60       # one-sentence key takeaway (~30 EN words)
CARD_TAKEAWAY_MAX = 25         # per §5.3 "max 25 words per card"
SECTION_ID_RE = re.compile(r"^[a-z0-9_-]+$")


# ── 1. input representation (Ingest output contract) ──────────────────────────

class DocumentRole(str, Enum):
    """Logical function of a conceptual block inside the source argument."""

    BACKGROUND = "BACKGROUND"
    PROBLEM = "PROBLEM"
    MOTIVATION = "MOTIVATION"
    DEFINITION = "DEFINITION"
    MECHANISM = "MECHANISM"
    EVIDENCE = "EVIDENCE"
    COMPARISON = "COMPARISON"
    CASE_STUDY = "CASE_STUDY"
    IMPLICATION = "IMPLICATION"
    LIMITATION = "LIMITATION"
    CONCLUSION = "CONCLUSION"


class StructureType(str, Enum):
    """Geometric organization of the elements inside a block/concept."""

    HIERARCHY = "HIERARCHY"            # classification tree
    PIPELINE = "PIPELINE"              # ordered process / lifecycle
    LAYERED_STACK = "LAYERED_STACK"    # architecture tiers
    QUADRANT = "QUADRANT"              # 2D position matrix
    CYCLIC = "CYCLIC"                  # feedback loop / state machine
    FUNNEL = "FUNNEL"                  # decreasing hierarchy (memory pyramid)
    MODULAR_CARDS = "MODULAR_CARDS"    # parallel discrete points


class Relationship(BaseModel):
    """Structured causal / systemic / comparative link."""

    model_config = _forbid()
    source: str
    relation: str = Field(
        description="causes | contains | depends_on | compared_with | precedes | degrades"
    )
    target: str
    supporting_refs: list[str] = Field(default_factory=list)


class SourceLocator(BaseModel):
    """Multi-dimensional physical provenance anchor.

    Forged line numbers are forbidden (§9.2): ``start_line``/``end_line`` come
    only from the deterministic extracted-text line map. Session transcripts
    keep the shipped ``message_id`` convention (``<!-- msg:UUID -->`` markers).
    """

    model_config = _forbid()
    doc_id: str
    page: Optional[int] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    bbox: Optional[list[float]] = None          # [x0, y0, x1, y1] in PDF points
    source_excerpt: Optional[str] = Field(
        default=None, description="short verbatim excerpt from the source"
    )
    message_id: Optional[str] = Field(
        default=None, description="session transcript locator (msg marker convention)"
    )

    @model_validator(mode="after")
    def _has_physical_anchor(self) -> "SourceLocator":
        if self.start_line is not None and self.end_line is not None:
            if self.end_line < self.start_line:
                raise ValueError("end_line before start_line")
        if not (
            self.page is not None
            or self.start_line is not None
            or self.message_id
            or self.bbox
        ):
            raise ValueError(
                "locator needs at least one physical anchor "
                "(page / start_line / message_id / bbox)"
            )
        return self


class Metric(BaseModel):
    """An explicit quantitative fact. Purely theoretical sections carry none —
    fabricating numbers is a hard contract violation."""

    model_config = _forbid()
    name: str
    value: Union[float, int, str]
    unit: Optional[str] = None
    locator: SourceLocator


class VisualAssetType(str, Enum):
    RASTER_IMAGE = "RASTER_IMAGE"
    VECTOR_REGION = "VECTOR_REGION"
    PAGE_FALLBACK_CROP = "PAGE_FALLBACK_CROP"


class PresentationWorth(str, Enum):
    HERO_ANCHOR = "HERO_ANCHOR"                 # high-value figure, 35%-60% focal area
    SUPPORTING_EVIDENCE = "SUPPORTING_EVIDENCE"  # small chart / detail card note
    DECORATIVE_NOISE = "DECORATIVE_NOISE"        # ignore when planning slides


class VisualAsset(BaseModel):
    """A physical slice extracted by Ingest. Provenance (page/bbox/path) must
    survive every downstream step (§9.3) — never re-invent a figure's origin."""

    model_config = _forbid()
    asset_id: str
    page: int
    type: VisualAssetType
    path: str
    bbox: Optional[list[float]] = None
    nearby_text: Optional[str] = None
    semantic_hint: Optional[str] = Field(
        default=None, description="caption/heading info captured during ingest"
    )


class TextBlock(BaseModel):
    model_config = _forbid()
    block_id: str
    text: str
    locator: SourceLocator


class DocumentRepresentation(BaseModel):
    """Standardized multimodal output of the Ingest stage (text + visuals)."""

    model_config = _forbid()
    doc_id: str
    document_title: str
    page_count: int
    text_blocks: list[TextBlock] = Field(default_factory=list)
    visual_assets: list[VisualAsset] = Field(default_factory=list)

    def asset_map(self) -> dict[str, VisualAsset]:
        return {a.asset_id: a for a in self.visual_assets}


# ── 2. cognition: per-asset and per-section understanding ─────────────────────

class VisualUnderstanding(BaseModel):
    """VLM semantic analysis of one sliced asset. Physical provenance is held
    by the ``asset_id`` link into ``DocumentRepresentation.visual_assets``."""

    model_config = _forbid()
    asset_id: str
    visual_type_detected: str = Field(
        description="ARCHITECTURE_DIAGRAM | FLOWCHART | BENCHMARK_PLOT | "
                    "SYSTEM_TAXONOMY | METAPHOR_ILLUSTRATION"
    )
    visual_summary: str = Field(description="one sentence: the mechanism/trend shown")
    extracted_labels: list[str] = Field(
        default_factory=list, description="module names, text labels, data values in the figure"
    )
    internal_topology: Optional[str] = Field(
        default=None, description="e.g. '7 stacked tiers top-down', 'circular 5-phase loop'"
    )
    supported_concepts: list[str] = Field(default_factory=list)
    presentation_worth: PresentationWorth
    recommended_grammar: str = Field(description="VisualGrammar name this figure satisfies")


class SectionUnderstanding(BaseModel):
    """Fixed 8 core cognitive dimensions + 1 document-role metadata."""

    model_config = _forbid()
    section_id: str
    section_title: str
    document_role: DocumentRole

    # 8 core dimensions
    main_idea: str = Field(description="central proposition (required)")
    problem_motivation: Optional[str] = None
    solution_approach: Optional[str] = None
    key_elements: list[str] = Field(default_factory=list)
    structure_type: StructureType = StructureType.MODULAR_CARDS
    relationships: list[Relationship] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    evidence_refs: list[SourceLocator] = Field(default_factory=list)
    visual_potential: Optional[str] = None

    matched_visual_asset_ids: list[str] = Field(default_factory=list)

    @field_validator("section_id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not SECTION_ID_RE.match(v):
            raise ValueError(f"section_id must match {SECTION_ID_RE.pattern}, got {v!r}")
        return v

    @field_validator("main_idea")
    @classmethod
    def _main_idea_present(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("main_idea is required")
        return v


class GlobalMentalModel(BaseModel):
    """High-level mental model produced by (hierarchical) reduce over sections."""

    model_config = _forbid()
    document_title: str
    executive_thesis: str
    key_themes: list[str] = Field(default_factory=list)
    major_problems: list[str] = Field(default_factory=list)
    major_solutions: list[str] = Field(default_factory=list)
    global_relationships: list[Relationship] = Field(default_factory=list)
    contradictions_and_tradeoffs: list[str] = Field(default_factory=list)
    critical_metrics: list[Metric] = Field(default_factory=list)
    sections: list[SectionUnderstanding] = Field(default_factory=list)
    visual_understandings: dict[str, VisualUnderstanding] = Field(
        default_factory=dict, description="indexed by asset_id"
    )

    def section_map(self) -> dict[str, SectionUnderstanding]:
        return {s.section_id: s for s in self.sections}

    def metric_map(self) -> dict[str, Metric]:
        # stable key: section-less critical metrics get index names at reduce time
        return {f"m{i}": m for i, m in enumerate(self.critical_metrics)}


# ── 3. canonical IR: PresentationBrief ────────────────────────────────────────

class EpistemicType(str, Enum):
    FACT = "FACT"                                 # must carry a SourceLocator
    CLAIM = "CLAIM"                               # author's explicit argument
    GROUNDED_SYNTHESIS = "GROUNDED_SYNTHESIS"     # must carry supporting_facts
    INTERPRETATION = "INTERPRETATION"             # narrative metaphor, never a FACT


class VisualGrammar(str, Enum):
    """Spatial semantics of a slide's content."""

    HERO_METAPHOR = "HERO_METAPHOR"
    SYSTEM_BLUEPRINT = "SYSTEM_BLUEPRINT"
    PIPELINE_FLOW = "PIPELINE_FLOW"
    TIMELINE = "TIMELINE"
    QUADRANT_MATRIX = "QUADRANT_MATRIX"
    INVERTED_PYRAMID = "INVERTED_PYRAMID"
    CIRCULAR_LOOP = "CIRCULAR_LOOP"
    DATA_CHART = "DATA_CHART"
    COMPARISON = "COMPARISON"
    TABLE = "TABLE"
    SOURCE_FIGURE_REUSE = "SOURCE_FIGURE_REUSE"
    ANNOTATED_FIGURE = "ANNOTATED_FIGURE"
    STRUCTURED_CARDS = "STRUCTURED_CARDS"
    TEXTUAL_THESIS = "TEXTUAL_THESIS"


class VisualGenerationPolicy(str, Enum):
    SOURCE_FIDELITY = "SOURCE_FIDELITY"           # reuse a source slice verbatim
    QUANTITATIVE_CODE = "QUANTITATIVE_CODE"       # deterministic code drawing, zero LLM
    EXPLANATORY_DIAGRAM = "EXPLANATORY_DIAGRAM"   # local template engine drawing


class TraceabilityNode(BaseModel):
    """One entry of the deck's epistemic ledger."""

    model_config = _forbid()
    trace_id: str
    epistemic_type: EpistemicType
    statement: str
    locator: Optional[SourceLocator] = None
    supporting_facts: list[str] = Field(default_factory=list)
    slide_ids: list[int] = Field(default_factory=list)
    visual_asset_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _epistemic_discipline(self) -> "TraceabilityNode":
        if self.epistemic_type == EpistemicType.FACT and self.locator is None:
            raise ValueError("FACT traceability node must carry a locator")
        if (
            self.epistemic_type == EpistemicType.GROUNDED_SYNTHESIS
            and not self.supporting_facts
        ):
            raise ValueError("GROUNDED_SYNTHESIS must list supporting_facts (trace ids)")
        return self


class VisualSpec(BaseModel):
    model_config = _forbid()
    visual_spec_id: str = Field(description="stable id for deterministic materialization")
    grammar: VisualGrammar
    policy: VisualGenerationPolicy
    semantic_intent: str = Field(description="the structural logic this visual conveys")
    has_dominant_anchor: bool = True
    reuse_asset_id: Optional[str] = None
    generation_spec: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def _policy_consistency(self) -> "VisualSpec":
        if self.grammar in (VisualGrammar.SOURCE_FIGURE_REUSE, VisualGrammar.ANNOTATED_FIGURE):
            if self.policy != VisualGenerationPolicy.SOURCE_FIDELITY:
                raise ValueError(f"{self.grammar.value} requires SOURCE_FIDELITY policy")
            if not self.reuse_asset_id:
                raise ValueError(f"{self.grammar.value} requires reuse_asset_id")
        elif self.reuse_asset_id:
            # pairing runs both ways: a rid on a structural grammar would be a
            # figure the template has no slot for — the renderer must never be
            # asked to silently drop an asset, so the combination is illegal.
            raise ValueError(
                f"reuse_asset_id is only legal on SOURCE_FIGURE_REUSE/"
                f"ANNOTATED_FIGURE grammars, not on {self.grammar.value}")
        if self.policy == VisualGenerationPolicy.QUANTITATIVE_CODE and not self.generation_spec:
            raise ValueError("QUANTITATIVE_CODE needs a generation_spec (labels/values/points)")
        return self


class SlideContentCard(BaseModel):
    model_config = _forbid()
    label: str
    takeaway: str
    metric_highlight: Optional[str] = None
    epistemic_type: EpistemicType
    trace_id: str = Field(description="TraceabilityNode.trace_id this card stands on")

    @field_validator("takeaway")
    @classmethod
    def _takeaway_budget(cls, v: str) -> str:
        units = text_units(v)
        if not 1 <= units <= CARD_TAKEAWAY_MAX:
            raise ValueError(f"takeaway is {units} units, budget is 1..{CARD_TAKEAWAY_MAX}")
        return v


class SlidePlan(BaseModel):
    model_config = _forbid()
    slide_index: int
    title: str
    subtitle: Optional[str] = None
    pedagogical_purpose: str = Field(
        description="PARADIGM_SHIFT | TAXONOMY | DEEP_DIVE | TRADE_OFF | EVIDENCE | SUMMARY …"
    )
    central_message: str
    source_section_ids: list[str] = Field(default_factory=list)
    visual_spec: VisualSpec
    cards: list[SlideContentCard] = Field(default_factory=list, max_length=4)
    speaker_notes: str = ""

    @field_validator("title")
    @classmethod
    def _title_budget(cls, v: str) -> str:
        units = text_units(v)
        if not 1 <= units <= TITLE_MAX:
            raise ValueError(f"title is {units} units, budget is 1..{TITLE_MAX}")
        return v

    @field_validator("central_message")
    @classmethod
    def _message_budget(cls, v: str) -> str:
        units = text_units(v)
        if not 1 <= units <= CENTRAL_MESSAGE_MAX:
            raise ValueError(
                f"central_message is {units} units, budget is 1..{CENTRAL_MESSAGE_MAX}"
            )
        return v

    @model_validator(mode="after")
    def _card_traces_resolve(self) -> "SlidePlan":
        # card.trace_id resolution against the brief graph is checked deck-wide by
        # qa (validators cannot see sibling fields of the parent model).
        return self


class PresentationBrief(BaseModel):
    """The canonical truth carrier of a deck (``brief.json``).

    Given this brief + the sliced assets, the Visual Compiler must be able to
    re-emit every derived artifact with **zero LLM calls** (§1.2.6).
    """

    model_config = _forbid()
    deck_id: str
    thesis: str
    target_audience: str
    target_slide_count: int
    presentation_style: str
    narrative_arc: str
    slides: list[SlidePlan] = Field(min_length=1)
    traceability_graph: dict[str, TraceabilityNode] = Field(default_factory=dict)

    @field_validator("target_slide_count")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(3, min(20, v))

    @model_validator(mode="after")
    def _deck_consistency(self) -> "PresentationBrief":
        idx = [s.slide_index for s in self.slides]
        if len(set(idx)) != len(idx):
            raise ValueError(f"duplicate slide_index: {idx}")
        if idx != sorted(idx):
            raise ValueError("slides must be ordered by slide_index")
        unknown: list[str] = []
        for s in self.slides:
            for c in s.cards:
                if c.trace_id and c.trace_id not in self.traceability_graph:
                    unknown.append(c.trace_id)
        if unknown:
            raise ValueError(
                f"card trace_ids not in traceability_graph: {sorted(set(unknown))}"
            )
        return self

    def slide_map(self) -> dict[int, SlidePlan]:
        return {s.slide_index: s for s in self.slides}

    def slides_with_anchor(self) -> list[SlidePlan]:
        return [s for s in self.slides if s.visual_spec.has_dominant_anchor]


# ── generation knobs (API-facing; kept from the shipped deck_options chain) ───

class PresentationControls(BaseModel):
    """What ``stage_generate`` builds from the request params.

    ``DeckOptions`` renamed to express its role: parameters for the cognitive
    workflow, not a rendering spec. Field names are wire-compatible with the
    jobs/tasks passthrough (count/audience/goal/language/format_mode/prompt).
    """

    model_config = _forbid()
    target_audience: str = ""
    presentation_goal: str = ""
    target_slide_count: int = 8
    language: str = ""                              # "" = follow source language
    format_mode: str = "detailed"                   # "detailed" | "presenter"
    user_guidance: str = ""

    @field_validator("target_slide_count")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(3, min(20, v))


# ── workflow configuration ────────────────────────────────────────────────────

class PresentationWorkflowConfig(BaseModel):
    model_config = _forbid()
    enable_visual_understanding: bool = True
    enable_hierarchical_reduce: bool = True
    reduce_group_threshold: int = 15          # sections above this trigger group-reduce
    max_vlm_assets: int = 24                  # cost guard: top-N slices per deck
    min_raster_side_px: int = 200             # smaller embedded images are noise
    min_cluster_w_pt: float = 120.0           # vector-cluster acceptance floor
    min_cluster_h_pt: float = 80.0
    cluster_tolerance_pt: float = 25.0        # drawing-rect merge padding
    high_density_drawings: int = 30           # page-fallback trigger thresholds
    high_density_images: int = 2
    section_chunk_max_chars: int = 8000      # conceptual-block packing budget (Pass A)
    pass_concurrency: int = 4                # in-stage LLM call concurrency (A/B)
    brief_max_turns: int = 8                  # cap VALUES: declared dims live in workflow_spec
    brief_max_no_progress: int = 2
    default_presentation_style: str = "Technical Masterclass"
    default_target_audience: str = "System Architects & Engineers"


# ── render report (kept from the shipped pipeline contract) ───────────────────

class RenderReport(BaseModel):
    model_config = _forbid()
    compiled: bool = False
    pages_expected: int = 0
    pages_actual: int = 0
    aspect_ok: bool = False
    missing_fonts: list[str] = Field(default_factory=list)
    typst_warnings: list[str] = Field(default_factory=list)
    layout_warnings: list[str] = Field(default_factory=list)
    overflow_suspect: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.compiled
            and self.pages_actual == self.pages_expected
            and self.aspect_ok
            and not self.missing_fonts
        )
