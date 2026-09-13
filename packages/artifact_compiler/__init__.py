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
from artifact_compiler.validators import (
    ValidationReport,
    validate_plan_references,
    validate_section_tree,
)
from artifact_compiler.visual import Asset

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
    "IllegalTransition",
    "Locator",
    "RunState",
    "SectionPlan",
    "ValidationReport",
    "VisualSpec",
    "WriterClaimOutput",
    "validate_plan_references",
    "validate_section_tree",
    "validate_transition",
]
