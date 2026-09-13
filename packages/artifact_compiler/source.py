"""Source-side data contracts: Locator, Evidence, Citation, Claim, Conflict.

The *write-authority split* is enforced structurally: a Writer produces
:class:`WriterClaimOutput`, which has NO grounding fields. Only the QA reducer may
promote a ``WriterClaimOutput`` into a full :class:`Claim` by attaching the
authoritative ``grounding_status`` / ``entailment_score`` (invariant 3).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class EvidenceType(str, Enum):
    fact = "fact"
    statistic = "statistic"
    definition = "definition"
    procedure = "procedure"
    opinion = "opinion"


class GroundingStatus(str, Enum):
    pending = "pending"
    supported = "supported"
    partially_supported = "partially_supported"
    unsupported = "unsupported"
    contradicted = "contradicted"


class ConflictType(str, Enum):
    numeric = "numeric"
    factual = "factual"
    temporal = "temporal"


class ConflictResolution(str, Enum):
    present_both = "present-both"
    unresolved = "unresolved"
    resolved_by_rule = "resolved_by_rule"


class Locator(BaseModel):
    """A strictly-resolvable pointer into a source. Must carry ≥1 concrete anchor so
    every Evidence can be traced back to bytes (invariant 2)."""

    page: int | None = None
    chunk_id: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _at_least_one_anchor(self) -> Locator:
        if self.page is None and self.chunk_id is None and self.url is None:
            raise ValueError("locator requires at least one of page, chunk_id or url")
        return self


class Evidence(BaseModel):
    evidence_id: str
    source_id: str
    locator: Locator
    excerpt: str = Field(min_length=1)
    evidence_type: EvidenceType


class Citation(BaseModel):
    citation_id: str
    evidence_ids: list[str] = Field(min_length=1)


class ClaimRequirement(BaseModel):
    """An *authoritative grounding set* for a section: the evidence that MUST back the
    claim(s) it spawns. ``evidence_ids`` min 1 keeps the topology from hollowing out."""

    claim_req_id: str
    section_id: str
    purpose: str
    evidence_ids: list[str] = Field(min_length=1)


class WriterClaimOutput(BaseModel):
    """The ONLY claim shape a Writer may emit — deliberately lacks grounding fields."""

    claim_id: str
    claim_req_id: str  # required: strong attribution, no orphan claims
    section_id: str
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    model_config = {"extra": "forbid"}  # reject a Writer sneaking in grounding_status


class Claim(WriterClaimOutput):
    """System-persisted claim: a WriterClaimOutput plus authoritative grounding."""

    grounding_status: GroundingStatus = GroundingStatus.pending
    entailment_score: float | None = Field(default=None, ge=0.0, le=1.0)


class EvidenceConflict(BaseModel):
    """A first-class relation entity (not a property of a single claim): at least two
    evidence rows pull in different directions on the cited claims."""

    conflict_id: str
    claim_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=2)
    type: ConflictType
    resolution: ConflictResolution
    resolution_reason: str


# Re-export guard: pydantic would accept a plain dict via model_validate; keep the
# Writer boundary honest by rejecting unknown keys on the Writer schema only.
assert "grounding_status" not in WriterClaimOutput.model_fields
