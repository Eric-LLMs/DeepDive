"""Intent Funnel node contracts — the vocabulary every node speaks.

P0 status (frozen discipline, see docs/temp.md "P0 施工纪律"): each node gets its
contract BEFORE an implementation is wired. Today only ``IntentVerdict`` and
``BoundArguments`` have producers — the funnel wraps the existing QIR
``RouteResult`` and ``actions.bind_arguments`` through them (adapter wiring,
zero behavior change). ``MatchResult`` / ``RecallResult`` / ``JudgeVerdict`` /
``DecisionResult`` are contract-only placeholders: producers arrive in P1
(Registry/Matcher) and P2 (Judge). No node may pass anything else across the
boundary, and per 8.8 a verdict carries routing metadata ONLY — never an
executor, tool instance or authorization bypass.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── Node 1: Matcher (P1 producer) ────────────────────────────────────────────────

MATCH_HIT = "HIT"
MATCH_MISS = "MISS"
MATCH_AMBIGUOUS = "MATCH_AMBIGUOUS"  # prefixed per 8.10; never the bare word


@dataclass(frozen=True)
class MatchResult:
    """Deterministic table match. AMBIGUOUS carries ALL candidate ids and the
    funnel escalates them upward (8.1) — the Matcher never picks one."""

    state: str = MATCH_MISS
    capability_id: str | None = None
    registry_version: str = ""
    candidates: tuple[str, ...] = ()
    # Which pattern/alias actually produced a single HIT (shadow telemetry —
    # the equivalence dataset needs the literal, not just the verdict).
    # Empty for MISS/AMBIGUOUS (no single answer to attribute).
    matched_literal: str = ""


# ── Node 2: Recall (P1 producer; today lives inside qir.semantic) ────────────────


@dataclass(frozen=True)
class Candidate:
    capability_id: str
    score: float
    matched_example: str = ""


@dataclass(frozen=True)
class RecallResult:
    """Top-k evidence only — recall proposes, it never disposes (design §3)."""

    candidates: tuple[Candidate, ...] = ()


# ── Node 3: Judge (P2 producer; no behavior exists yet) ──────────────────────────

JUDGE_CONFIDENT = "CONFIDENT"
JUDGE_UNCERTAIN = "UNCERTAIN"
JUDGE_REJECT = "REJECT"


@dataclass(frozen=True)
class JudgeVerdict:
    decision: str = JUDGE_UNCERTAIN
    capability_id: str | None = None
    rationale: str = field(default="", repr=False)


# ── Node 4: Decision LLM (P2 producer; today lives inside qir.decision) ──────────


@dataclass(frozen=True)
class DecisionResult:
    capability_id: str | None  # None == NONE -> Agent
    rationale: str = field(default="", repr=False)


# ── Binder (wired in P0 over the existing actions.bind_arguments) ────────────────

BIND_COMPLETE = "COMPLETE"
BIND_MISSING = "MISSING"
# BIND_AMBIGUOUS / BIND_INVALID arrive with the binder rewrite (P1+); the four
# states never collapse into a plain None (8.7): "no arguments" is a STATE.

@dataclass(frozen=True)
class BoundArguments:
    state: str
    args: dict | None = None

    @classmethod
    def of(cls, args: dict | None) -> "BoundArguments":
        """Adapter over the legacy ``bind_arguments`` return: dict -> COMPLETE,
        None -> MISSING (the existing C1 abstain shape, unchanged)."""
        return cls(BIND_COMPLETE, args) if args is not None else cls(BIND_MISSING, None)

    @property
    def is_complete(self) -> bool:
        return self.state == BIND_COMPLETE and self.args is not None

    @property
    def is_missing(self) -> bool:
        return self.state == BIND_MISSING


# ── Funnel output (wired in P0 over the existing qir.RouteResult) ────────────────


@dataclass(frozen=True)
class IntentVerdict:
    """Routing metadata ONLY (8.8): which capability, from which registry
    version, decided at which stage. Execution permission lives nowhere here."""

    capability_id: str
    registry_version: str
    stage: str = "semantic+decision"

    @classmethod
    def from_qir(cls, route_result) -> "IntentVerdict":
        """Adapter over the legacy ``qir.types.RouteResult`` (same fields)."""
        return cls(
            capability_id=route_result.capability_id,
            registry_version=route_result.registry_version,
            stage=getattr(route_result, "stage", "semantic+decision"),
        )


@dataclass(frozen=True)
class AgentFallback:
    """The zero-pollution exit: the funnel abstained, the Agent keeps the turn
    and receives the user's text BYTE-IDENTICAL (8.10)."""

    reason: str = ""  # prefixed reason code (8.10), e.g. DECISION_NONE
