"""Research Artifact Compiler — the deterministic, zero-LLM PDF core.

Normative spec: ``docs/research/19-pdf-artifact-compiler.md``.

Layer rule (invariant 1): this package contains NO LLM call and NO network client.
It is directly usable from CLI, async jobs or the HTTP API without the plugin or
skill layers. Semantic decisions (writing, judging, repair-regeneration) belong to
the Skill/agent layer, which feeds validated payloads in through :mod:`runstore`.
"""

from artifact_compiler.plan import (
    ArtifactPlan,
    GenerationBudget,
    SectionPlan,
    VisualSpec,
)
from artifact_compiler.projection import ProjectionError, project_manuscript_to_ast
from artifact_compiler.qa import GroundingDiagnosis, GroundingOutcome, reduce_grounding
from artifact_compiler.repair import PatchError, apply_patch
from artifact_compiler.source import (
    Citation,
    Claim,
    ClaimRequirement,
    Evidence,
    EvidenceConflict,
    Locator,
    WriterClaimOutput,
)
from artifact_compiler.states import (
    PUBLISHABLE_STATES,
    IllegalTransition,
    RunState,
    validate_transition,
)
from artifact_compiler.typst_compiler import compile_typst, load_default_template
from artifact_compiler.validators import (
    ValidationReport,
    validate_plan_references,
    validate_section_tree,
)
from artifact_compiler.visual import Asset
from artifact_compiler.visual_engine import density_issues, sanitize_svg

__all__ = [
    "PUBLISHABLE_STATES",
    "ArtifactPlan",
    "Asset",
    "Citation",
    "Claim",
    "ClaimRequirement",
    "Evidence",
    "EvidenceConflict",
    "GenerationBudget",
    "GroundingDiagnosis",
    "GroundingOutcome",
    "IllegalTransition",
    "Locator",
    "PatchError",
    "ProjectionError",
    "RunState",
    "SectionPlan",
    "ValidationReport",
    "VisualSpec",
    "WriterClaimOutput",
    "apply_patch",
    "compile_typst",
    "density_issues",
    "load_default_template",
    "project_manuscript_to_ast",
    "reduce_grounding",
    "sanitize_svg",
    "validate_plan_references",
    "validate_section_tree",
    "validate_transition",
]
