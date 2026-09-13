"""Plan-side data contracts: VisualSpec, SectionPlan, budget, ArtifactPlan.

Produced by the planning step (an agent/Skill decision) and then *frozen*; the Core
only validates and consumes it. ``expected_blocks`` are REQUIRED blocks — each type
must appear ≥1× in the section's AST, and a ``figure`` counts only when its Asset
compiled successfully (invariant 5).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from artifact_compiler.source import ClaimRequirement


class Renderer(str, Enum):
    mermaid = "mermaid"
    svg = "svg"
    vega = "vega"


class VisualFormat(str, Enum):
    flowchart = "flowchart"
    architecture = "architecture"
    sequence = "sequence"
    comparison_matrix = "comparison-matrix"


class LayoutDirective(str, Enum):
    top_to_bottom = "top-to-bottom"
    left_to_right = "left-to-right"


class ContentMode(str, Enum):
    explanation = "explanation"
    comparison = "comparison"
    analysis = "analysis"
    synthesis = "synthesis"
    procedure = "procedure"
    summary = "summary"


class BlockKind(str, Enum):
    paragraph = "paragraph"
    callout = "callout"
    figure = "figure"
    table = "table"
    list = "list"


class Density(str, Enum):
    compact = "compact"
    standard = "standard"
    academic = "academic"


class Theme(str, Enum):
    tech_report = "tech-report"
    minimal_mono = "minimal-mono"


class VisualConstraints(BaseModel):
    max_nodes: int = 12
    allow_cross_edges: bool = False


class VisualRelationship(BaseModel):
    from_id: str = Field(alias="from")
    to_id: str = Field(alias="to")
    label: str | None = None

    model_config = {"populate_by_name": True}


class VisualSpec(BaseModel):
    spec_id: str
    renderer: Renderer = Renderer.mermaid
    visual_format: VisualFormat
    purpose: str
    layout_directive: LayoutDirective = LayoutDirective.top_to_bottom
    entities: list[str] = Field(min_length=1)
    relationships: list[VisualRelationship] = Field(default_factory=list)
    constraints: VisualConstraints = Field(default_factory=VisualConstraints)
    # Strong traceability: a figure with no claims/evidence is a decoration, not research.
    claim_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class SectionPlan(BaseModel):
    section_id: str
    parent_id: str | None = None
    order: int = Field(ge=0)
    title: str
    content_mode: ContentMode
    expected_blocks: list[BlockKind] = Field(default_factory=list)
    claim_requirements: list[ClaimRequirement] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)  # retrieval hints only
    visual_spec: VisualSpec | None = None


class GenerationBudget(BaseModel):
    max_llm_calls: int = 100
    max_input_tokens: int = 400_000
    max_output_tokens: int = 100_000
    max_runtime_ms: int = 900_000  # 15 min
    max_repair_attempts: int = 3


class ArtifactMetadata(BaseModel):
    title: str
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list)
    target_audience: str = ""
    language: str = "zh-CN"
    keywords: list[str] = Field(default_factory=list)


class SourceScope(BaseModel):
    source_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class StyleConfig(BaseModel):
    density: Density = Density.standard
    theme: Theme = Theme.tech_report


class SummarySpec(BaseModel):
    purpose: str
    core_questions: list[str] = Field(default_factory=list)


class ArtifactPlan(BaseModel):
    artifact_id: str
    artifact_revision: int = Field(default=1, ge=1)
    schema_version: int = 5
    metadata: ArtifactMetadata
    source_scope: SourceScope
    budget: GenerationBudget = Field(default_factory=GenerationBudget)
    style_config: StyleConfig = Field(default_factory=StyleConfig)
    summary_spec: SummarySpec
    sections: list[SectionPlan] = Field(min_length=1)
