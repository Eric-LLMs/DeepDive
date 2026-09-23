"""QIR data contracts: routing metadata only — never execution, never arguments.

Pipeline ownership (frozen):

    resolve_plan
      (1) QIR              Query -> sentence-level Intent -> Capability ID
      (2) Argument Binding Capability + Query -> structured arguments   [actions.py]
      (3) build_execution_plan -> PlanKind / Executor                   [existing]

This module is the vocabulary of stage (1) only. ``RouteResult`` deliberately
carries NOTHING an executor needs beyond ``capability_id + registry_version``:
arguments, schemas and tool dispatch all live outside the QIR package.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict


class SnapshotError(Exception):
    """Draft validation / immutable-snapshot build failure. A failed build must
    never become Active — the previous snapshot keeps serving (no half-ready state)."""


@dataclass(frozen=True)
class Capability:
    """One routable user-facing capability. Routing metadata ONLY — it is NOT a
    second Tool Registry: ``tool_binding`` must name a tool the existing runtime
    already registers, and it must exist in the existing ``DIRECT_TOOLS`` action
    binding table (validated at snapshot build time)."""

    id: str
    tool_binding: str
    description: str
    examples: tuple[str, ...]
    negatives: tuple[str, ...] = ()
    enabled: bool = True


@dataclass(frozen=True)
class ExampleVector:
    """One pre-computed example embedding, grouped by capability. Derived data:
    built together with the snapshot, swapped atomically with it."""

    capability_id: str
    example_index: int
    vector: tuple[float, ...]


@dataclass(frozen=True)
class Snapshot:
    """Immutable published routing projection. ``version`` is a content
    fingerprint — any RouteResult stamps it and the executor re-validates it
    right before dispatch (route/execute TOCTOU defense)."""

    version: str
    built_at: float
    capabilities: tuple[Capability, ...]
    example_vectors: tuple[ExampleVector, ...] = ()

    def get(self, capability_id: str) -> Capability | None:
        for cap in self.capabilities:
            if cap.id == capability_id:
                return cap
        return None

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "built_at": self.built_at,
            "capabilities": [asdict(c) for c in self.capabilities],
            "example_vectors": [asdict(v) for v in self.example_vectors],
        }

    @classmethod
    def from_json(cls, raw: dict) -> "Snapshot":
        return cls(
            version=str(raw["version"]),
            built_at=float(raw.get("built_at") or 0.0),
            capabilities=tuple(
                Capability(
                    id=str(c["id"]),
                    tool_binding=str(c["tool_binding"]),
                    description=str(c["description"]),
                    examples=tuple(str(e) for e in c.get("examples") or ()),
                    negatives=tuple(str(n) for n in c.get("negatives") or ()),
                    enabled=bool(c.get("enabled", True)),
                )
                for c in raw.get("capabilities") or ()
            ),
            example_vectors=tuple(
                ExampleVector(
                    capability_id=str(v["capability_id"]),
                    example_index=int(v["example_index"]),
                    vector=tuple(float(x) for x in v["vector"]),
                )
                for v in raw.get("example_vectors") or ()
            ),
        )


def fingerprint(capabilities: tuple[Capability, ...]) -> str:
    """Content-address the capability set (mirrors the workflow ``wf1-`` doctrine:
    structure is hashed in, config is not)."""
    canon = json.dumps(
        [asdict(c) for c in sorted(capabilities, key=lambda c: c.id)],
        ensure_ascii=False, sort_keys=True,
    )
    return "qir1-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class SemanticCandidate:
    """Candidate evidence produced by coarse retrieval. NEVER an execution trigger:
    high similarity alone must never dispatch — only the Decision stage can route."""

    capability_id: str
    score: float
    top_example: str


@dataclass(frozen=True)
class DecisionVerdict:
    """Final adjudication. ``capability_id is None`` == abstain (NONE)."""

    capability_id: str | None
    rationale: str = ""


@dataclass(frozen=True)
class RouteResult:
    """The ONLY thing stage (1) hands to stage (2): which capability, from which
    registry version. No arguments, no tool callables, no execution handles."""

    capability_id: str
    registry_version: str
    stage: str = "semantic+decision"
    abstain_reason: str = field(default="", repr=False)
