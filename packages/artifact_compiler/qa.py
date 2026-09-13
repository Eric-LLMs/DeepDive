"""QA reducer & final verdict (Layer 2 authoritative write; docs/research/19 §9).

The judge (an LLM) runs in the Skill/agent layer and submits a
:class:`GroundingDiagnosis`. Core's job is purely to **validate, reduce and
persist**: every claim gets exactly one verdict, statuses/scores are range-checked,
and only then may authoritative fields land on :class:`Claim`. Core never calls a
judge and never fabricates a verdict.

``decide_verdict`` maps the three QA layers onto the transient
``pass/needs_review/repair/blocked`` outcomes; RunState terminals
(``COMPLETED``/``NEEDS_REVIEW``/``FAILED_BLOCKED``) are written by the driver via
the RunStore, keeping verdicts and persisted states cleanly separated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from artifact_compiler.source import Claim, GroundingStatus
from artifact_compiler.validators import ValidationReport

Verdict = Literal["pass", "needs_review", "repair", "blocked"]

#: statuses that block a clean COMPLETED
_NOT_FULL = {
    GroundingStatus.partially_supported,
    GroundingStatus.unsupported,
    GroundingStatus.contradicted,
}


class GroundingVerdictEntry(BaseModel):
    claim_id: str
    grounding_status: GroundingStatus
    entailment_score: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str | None = None

    model_config = {"extra": "forbid"}


class GroundingDiagnosis(BaseModel):
    """The external caller's complete judgement for one QA round."""

    qa_run_id: str
    judge_model: str = Field(min_length=1)
    judge_prompt_version: str = Field(min_length=1)
    threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    verdicts: list[GroundingVerdictEntry] = Field(min_length=1)

    model_config = {"extra": "forbid"}


@dataclass
class GroundingOutcome:
    ok: bool
    errors: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)      # authoritative, rewritten
    record: dict = field(default_factory=dict)             # payload for qa/grounding.json

    @property
    def all_supported(self) -> bool:
        return self.ok and all(
            c.grounding_status == GroundingStatus.supported for c in self.claims
        )

    @property
    def flagged_claim_ids(self) -> list[str]:
        return [
            c.claim_id for c in self.claims
            if c.grounding_status in _NOT_FULL
        ]


def reduce_grounding(claims: list[Claim], diagnosis: GroundingDiagnosis) -> GroundingOutcome:
    """Validate the diagnosis against the claim set and write authoritative fields.

    Rejection rules (all errors collected, nothing partially applied):
    * every claim has EXACTLY one verdict; no verdict for an unknown claim;
    * ``pending`` is not an admissible final status;
    * a score below ``threshold`` with ``supported`` status is contradictory."""
    by_claim = {c.claim_id: c for c in claims}
    errors: list[str] = []
    seen: set[str] = set()
    for v in diagnosis.verdicts:
        if v.claim_id not in by_claim:
            errors.append(f"verdict for unknown claim {v.claim_id!r}")
            continue
        if v.claim_id in seen:
            errors.append(f"duplicate verdict for claim {v.claim_id!r}")
            continue
        seen.add(v.claim_id)
        if v.grounding_status == GroundingStatus.pending:
            errors.append(f"claim {v.claim_id!r} verdict is 'pending' (not final)")
        if (
            v.entailment_score is not None
            and v.grounding_status == GroundingStatus.supported
            and v.entailment_score < diagnosis.threshold
        ):
            errors.append(
                f"claim {v.claim_id!r}: score {v.entailment_score} < "
                f"threshold {diagnosis.threshold} but status 'supported'"
            )
    missing = sorted(set(by_claim) - seen)
    if missing:
        errors.append(f"claims without verdict: {missing}")
    if errors:
        return GroundingOutcome(ok=False, errors=errors)

    updated = [
        by_claim[v.claim_id].model_copy(
            update={
                "grounding_status": v.grounding_status,
                "entailment_score": v.entailment_score,
            }
        )
        for v in diagnosis.verdicts
    ]
    record = {
        "qa_run_id": diagnosis.qa_run_id,
        "judge_model": diagnosis.judge_model,
        "judge_prompt_version": diagnosis.judge_prompt_version,
        "threshold": diagnosis.threshold,
        "claims_total": len(updated),
        "by_status": _status_histogram(updated),
        "flagged_claim_ids": [
            c.claim_id for c in updated if c.grounding_status in _NOT_FULL
        ],
        "verdicts": [v.model_dump(mode="json") for v in diagnosis.verdicts],
    }
    return GroundingOutcome(ok=True, claims=updated, record=record)


def _status_histogram(claims: list[Claim]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for c in claims:
        key = c.grounding_status.value
        hist[key] = hist.get(key, 0) + 1
    return dict(sorted(hist.items()))


def decide_verdict(
    *,
    contract: ValidationReport,
    grounding: GroundingOutcome | None,
    rendered_issues: list[str] | None = None,
    repair_attempts: int = 0,
    max_repair_attempts: int = 3,
) -> Verdict:
    """Pure reduction of the three layers into one transient outcome.

    Priority: contract (pre-render) > rendered (Layer 1/3 structural) > grounding.
    Grounding gaps never enter the repair loop — production policy is
    ``NEEDS_REVIEW`` with a warnings banner (docs/research/19 §5/§9)."""
    if not contract.ok:
        return "blocked" if repair_attempts >= max_repair_attempts else "repair"
    if rendered_issues:
        return "blocked" if repair_attempts >= max_repair_attempts else "repair"
    if grounding is None or not grounding.ok:
        return "blocked" if repair_attempts >= max_repair_attempts else "repair"
    if grounding.all_supported:
        return "pass"
    return "needs_review"
