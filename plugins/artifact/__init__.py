"""plugins/artifact — the PDF path for the Research OS publish flow (docs/19 §10).

Thin surface only: argument validation → PrincipalContext → the deterministic
core (``packages/artifact_compiler``) → ArtifactRef / status JSON. No parsing,
no rendering logic, no QA logic lives here — those are Core; no LLM lives here
— the default path is the frozen manuscript projection (inv. 11).
"""
from plugins.artifact.service import ArtifactCompileService

__all__ = ["ArtifactCompileService"]
