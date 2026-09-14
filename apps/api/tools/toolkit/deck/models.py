"""Content-to-Slides deck engine: Pydantic models + budgets (docs/content-to-slides.md §3).

Every structure here is a *semantic* contract between pipeline passes. The LLM produces
Pass A/B/C JSON that is validated by these models; visual type and geometry are derived
downstream by pure functions (:mod:`.rules`, :mod:`.layout`) and can never be authored by
the model. ``extra="forbid"`` everywhere keeps the model boundary closed — an invented
field is a validation error, which feeds the corrective-retry path.

Lifecycle contract (errata #1/#2/#6):
  Pass A (UNDERSTAND)  — raw sources → ContentDigest (facts + quantities, with locators)
  Pass B (OUTLINE)     — digest only (NEVER the raw source) → Outline; visual-type-pure
  Pass C (EXPANSION)   — outline items + the digest FACT SUBSET they reference (never the
                         raw source) → Slides
  Pass D (VISUAL PLAN) — deterministic, no LLM: rules.derive_visual_plan
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── vocabularies (closed enums; prompts mirror them verbatim) ─────────────────

Purpose = Literal[
    "PROBLEM", "DEFINITION", "PROCESS", "COMPARISON",
    "TIMELINE", "ARCHITECTURE", "DATA_INSIGHT", "SUMMARY",
]
Relationship = Literal[
    "sequential", "comparative", "hierarchical",
    "categorical", "quantitative", "singular_takeaway",
]
VisualType = Literal[
    "TEXT_HERO", "CARDS", "FLOWCHART", "TIMELINE",
    "COMPARISON", "ARCHITECTURE", "CHART",
]
NarrativeStrategy = Literal[
    "problem_solution", "concept_map", "chronological", "comparative", "top_down",
]
LocatorScheme = Literal["message", "page", "line", "time"]


class SourceKind(str, Enum):
    session = "session"
    document = "document"
    book = "book"
    subtitles = "subtitles"


# ── text measurement units (shared by every budget check) ─────────────────────

_CJK_RE = re.compile(r"[\u2e80-\u3131\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-.]*")


def text_units(text: str) -> int:
    """Mixed-script word budget unit: one per CJK char, one per Latin/number word.

    ``"检索增强 生成 RAG pipeline"`` → 4 CJK chars + 2 Latin words = 6 units. This is the
    single yardstick every per-type budget in :mod:`.rules` is expressed against.
    """
    cjk = len(_CJK_RE.findall(text or ""))
    latin = len(_WORD_RE.findall(_CJK_RE.sub(" ", text or "")))
    return cjk + latin


def _forbid(**kwargs) -> ConfigDict:
    return ConfigDict(extra="forbid", **kwargs)


# ── source abstraction & provenance ───────────────────────────────────────────

class ProvenanceRef(BaseModel):
    """Where a fact came from. One locator field must be set for the ref to be usable."""

    model_config = _forbid()
    source_id: str = ""
    kind: SourceKind = SourceKind.document
    message_id: str | None = None
    page: int | None = None
    lines: str | None = None      # "start-end" — the existing [file:start-end] convention
    t_ms: int | None = None       # subtitle cue start
    quote: str | None = None

    @field_validator("lines")
    @classmethod
    def _lines_shape(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"\d+(-\d+)?", v):
            raise ValueError(f"lines must be 'start' or 'start-end', got {v!r}")
        return v


class SourceDoc(BaseModel):
    """One ingested input. MVP runs with len(sources)==1; the model is already N-ready."""

    model_config = _forbid()
    source_id: str
    kind: SourceKind
    name: str
    locators: list[LocatorScheme] = Field(default_factory=lambda: ["line"])


# ── Pass A: content digest ────────────────────────────────────────────────────

class Fact(BaseModel):
    model_config = _forbid()
    fact_id: str                       # "f1"… — referenced by outline fact_refs
    statement: str
    provenance: list[ProvenanceRef] = Field(min_length=1)   # no fact without a source
    superseded_by: str | None = None   # a later correction wins: this fact is dropped

    @field_validator("provenance")
    @classmethod
    def _has_locator(cls, v: list[ProvenanceRef]) -> list[ProvenanceRef]:
        for ref in v:
            if ref.message_id or ref.page or ref.lines or ref.t_ms is not None:
                break
        else:
            raise ValueError("every provenance ref needs a locator "
                             "(message_id / page / lines / t_ms)")
        return v


class Quantitative(BaseModel):
    """A real numeric fact. CHART points may only cite one of these (anti-fabrication)."""

    model_config = _forbid()
    quant_id: str                      # "q1"… — referenced by payload series point quant_ref
    metric: str
    value: float
    unit: str | None = None
    as_of: str | None = None
    provenance: list[ProvenanceRef] = Field(min_length=1)


class ContentDigest(BaseModel):
    """Pass A output: the clean, grounded fact base every later pass may rely on."""

    model_config = _forbid()
    title: str = ""
    facts: list[Fact] = Field(min_length=1)
    concepts: list[str] = Field(default_factory=list)
    quantities: list[Quantitative] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids_unique(self) -> "ContentDigest":
        fids = [f.fact_id for f in self.facts]
        if len(set(fids)) != len(fids):
            raise ValueError("duplicate fact_id in digest")
        qids = [q.quant_id for q in self.quantities]
        if len(set(qids)) != len(qids):
            raise ValueError("duplicate quant_id in digest")
        return self

    # live facts only: a superseded fact is a superseded fact (conversation corrections)
    def live_facts(self) -> list[Fact]:
        dropped = {f.superseded_by for f in self.facts if f.superseded_by}
        return [f for f in self.facts if not f.superseded_by]

    def fact_map(self) -> dict[str, Fact]:
        return {f.fact_id: f for f in self.facts}

    def quant_map(self) -> dict[str, Quantitative]:
        return {q.quant_id: q for q in self.quantities}


# ── Pass B: outline (visual-type-pure) ───────────────────────────────────────

class SlideOutlineItem(BaseModel):
    model_config = _forbid()
    slide_id: str                      # stable, e.g. "s3"
    title: str
    purpose: Purpose
    relationship: Relationship
    key_message: str                   # exactly one per slide
    fact_refs: list[str] = Field(default_factory=list)  # fact_ids this slide stands on

    @field_validator("key_message")
    @classmethod
    def _key_msg_budget(cls, v: str) -> str:
        units = text_units(v)
        if not 1 <= units <= KEY_MESSAGE_MAX:
            raise ValueError(f"key_message is {units} units, budget is 1..{KEY_MESSAGE_MAX}")
        return v


class SectionOutline(BaseModel):
    model_config = _forbid()
    title: str
    purpose: str
    slides: list[SlideOutlineItem] = Field(min_length=1)


class Outline(BaseModel):
    """Pass B output — the persisted, user-editable artifact.

    Contains NO visual-type assertion: shape selection belongs exclusively to Pass D.
    """

    model_config = _forbid()
    title: str
    narrative_strategy: NarrativeStrategy
    sections: list[SectionOutline] = Field(min_length=1)

    def all_slides(self) -> list[SlideOutlineItem]:
        return [s for sec in self.sections for s in sec.slides]


# outline-level semantic checks that need the target / digest context live in a pure
# function (Pydantic validators cannot see external arguments):
KEY_MESSAGE_MAX = 60        # ~30 EN words or ~60 CJK chars
TITLE_MAX = 12


def check_outline(outline: Outline, target_slide_count: int,
                  digest: ContentDigest) -> list[str]:
    """Return human-readable errors ([] = valid). Rules per docs §3.2:

    - ``target_slide_count`` counts CONTENT slides only (no cover/dividers/appendix);
      total must be within [target-2, target+2].
    - unique slide ids; every fact_ref resolves to a live digest fact;
    - at least one SUMMARY closing slide (the LAST slide);
    - never checks visual types (that is Pass D).
    """
    errs: list[str] = []
    slides = outline.all_slides()
    ids = [s.slide_id for s in slides]
    if len(set(ids)) != len(ids):
        errs.append("duplicate slide_id in outline")
    lo, hi = target_slide_count - 2, target_slide_count + 2
    if not lo <= len(slides) <= hi:
        errs.append(
            f"outline has {len(slides)} content slides, target is "
            f"{target_slide_count} (allowed {lo}..{hi})"
        )
    live = set(digest.fact_map())
    for s in slides:
        unknown = [r for r in s.fact_refs if r not in live]
        if unknown:
            errs.append(f"slide {s.slide_id}: fact_refs not in digest: {unknown}")
    if slides and slides[-1].purpose != "SUMMARY":
        errs.append("the last outline slide must have purpose SUMMARY (closing slide)")
    return errs


# ── Pass C: slide semantic model ──────────────────────────────────────────────

class Item(BaseModel):
    """A CARDS card, an ARCHITECTURE node (``group`` = tier), or a COMPARISON-adjacent
    concept. Pure semantics: no style/geometry fields are accepted (extra="forbid")."""

    model_config = _forbid()
    label: str
    detail: str = ""
    group: str = ""                  # set ⇒ belongs to an architecture tier
    provenance: list[ProvenanceRef] = Field(default_factory=list)


class Step(BaseModel):
    model_config = _forbid()
    label: str
    detail: str = ""
    when: str = ""                   # timeline events only: date/phase text


class Point(BaseModel):
    model_config = _forbid()
    x: str                           # category/label axis (dates as text)
    y: float
    quant_ref: str                   # MUST resolve in digest.quantities


class Series(BaseModel):
    model_config = _forbid()
    name: str
    points: list[Point]


class Column(BaseModel):
    model_config = _forbid()
    header: str
    cells: list[str]


class ContentPayload(BaseModel):
    """Shape-agnostic semantic slots. Exactly ONE shape group may be populated —
    a slide carries a single cognitive task. The slide's message itself lives on
    :attr:`Slide.key_message` (single source, never duplicated here)."""

    model_config = _forbid()
    items: list[Item] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    series: list[Series] = Field(default_factory=list)
    columns: list[Column] = Field(default_factory=list)

    @model_validator(mode="after")
    def _single_shape(self) -> "ContentPayload":
        shapes = [
            bool(self.items), bool(self.steps), bool(self.series), bool(self.columns),
        ]
        if sum(shapes) > 1:
            raise ValueError(
                "payload must carry ONE shape "
                f"(items/steps/series/columns populated: {shapes})"
            )
        if self.series:
            if len(self.series) > 2:
                raise ValueError("CHART allows at most 2 series")
            for s in self.series:
                if not 2 <= len(s.points) <= 8:
                    raise ValueError(f"series {s.name!r} needs 2..8 points")
        if self.columns:
            if len(self.columns) > 4:
                raise ValueError("COMPARISON allows at most 4 columns")
            lens = {len(c.cells) for c in self.columns}
            if len(lens) != 1 or next(iter(lens)) > 6:
                raise ValueError("comparison columns must have equal cell counts (<= 6 rows)")
        if len(self.items) > 6 or len(self.steps) > 8:
            raise ValueError("payload exceeds sanity caps (items<=6, steps<=8)")
        return self

    def shape(self) -> str:
        if self.series:
            return "series"
        if self.columns:
            return "columns"
        if self.steps:
            return "steps"
        if self.items:
            return "items-grouped" if any(i.group for i in self.items) else "items"
        return "message-only"


class Slide(BaseModel):
    """One slide's meaning — zero visual information."""

    model_config = _forbid()
    slide_id: str
    title: str
    key_message: str
    purpose: Purpose
    relationship: Relationship
    payload: ContentPayload
    speaker_notes: str = ""
    provenance_refs: list[ProvenanceRef] = Field(default_factory=list)

    @field_validator("key_message")
    @classmethod
    def _key_msg_budget(cls, v: str) -> str:
        units = text_units(v)
        if not 1 <= units <= KEY_MESSAGE_MAX:
            raise ValueError(f"key_message is {units} units, budget is 1..{KEY_MESSAGE_MAX}")
        return v

    @field_validator("title")
    @classmethod
    def _title_budget(cls, v: str) -> str:
        units = text_units(v)
        if units > TITLE_MAX:
            raise ValueError(f"title is {units} units, budget is <= {TITLE_MAX}")
        return v


# ── Pass D: visual plan (rule-derived; high-level intent only, never coordinates) ──

class LayoutIntent(BaseModel):
    model_config = _forbid()
    direction: Literal["horizontal", "vertical"] = "vertical"
    density: Literal["compact", "normal", "spacious"] = "normal"
    emphasis: Literal["primary", "neutral", "muted"] = "neutral"


class VisualPlan(BaseModel):
    model_config = _forbid()
    slide_id: str
    visual_type: VisualType
    intent: LayoutIntent = Field(default_factory=LayoutIntent)
    rationale: str = ""              # which fallback rule fired (debuggable)


# ── deck (top level) ──────────────────────────────────────────────────────────

class DeckOptions(BaseModel):
    """Generation request knobs (API-facing; all optional)."""

    model_config = _forbid()
    target_audience: str = ""
    presentation_goal: str = ""
    target_slide_count: int = 8

    @field_validator("target_slide_count")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(3, min(20, v))


class DeckSpec(BaseModel):
    """The single source of truth of a deck: everything downstream renders from it."""

    model_config = _forbid()
    deck_id: str
    title: str
    target_audience: str = ""
    presentation_goal: str = ""
    target_slide_count: int
    narrative_strategy: NarrativeStrategy
    sources: list[SourceDoc] = Field(min_length=1)   # MVP enforces len==1
    digest: ContentDigest
    outline: Outline
    slides: list[Slide]
    visual_plan: list[VisualPlan]
    section_dividers: bool = False                   # MVP default OFF
    speaker_notes_appendix: bool = False             # MVP default OFF (notes stay in deck.json)

    @model_validator(mode="after")
    def _consistency(self) -> "DeckSpec":
        oids = [s.slide_id for s in self.outline.all_slides()]
        sids = [s.slide_id for s in self.slides]
        if oids != sids:
            raise ValueError(
                f"slides must derive from the outline in order: outline={oids} slides={sids}"
            )
        pids = [p.slide_id for p in self.visual_plan]
        if pids != sids:
            raise ValueError("every slide needs a visual plan entry, in order")
        return self

    # pages_expected formula (docs §6, normative):
    def pages_expected(self) -> int:
        return (
            1                                    # cover
            + len(self.slides)                   # content slides
            + (len(self.outline.sections) if self.section_dividers else 0)
            + (1 if self.speaker_notes_appendix else 0)
        )


# ── render report (docs §6) ───────────────────────────────────────────────────

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


class LayoutOverflow(Exception):
    """Raised when a slide cannot fit its slot even at the smallest size tier.

    Loud failure by contract (errata #4): never a silent trim."""
