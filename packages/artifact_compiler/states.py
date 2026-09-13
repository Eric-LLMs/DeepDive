"""Compiler run state machine with complete terminal coverage.

Terminal-state discipline is the whole point: every path must land on one of the five
terminals — a run may never hang. ``QA_PASSED`` / ``QA_FAILED`` are *transient
verdicts* consumed by the transition table; they are deliberately NOT states
(invariant 10 in docs/research/19-pdf-artifact-compiler.md).
"""
from __future__ import annotations

import enum


class RunState(str, enum.Enum):
    # active pipeline stages
    QUEUED = "queued"
    ENV_PREFLIGHT = "env_preflight"
    EVIDENCE_PROVIDING = "evidence_providing"
    PLANNING = "planning"
    WRITING = "writing"
    AST_CONTRACT_QA = "ast_contract_qa"
    VISUAL_ENGINE = "visual_engine"
    TYPST_COMPILING = "typst_compiling"
    REPAIR_LOOP = "repair_loop"
    # terminals
    COMPLETED = "completed"                 # grounding 100%; PDF publishable
    NEEDS_REVIEW = "needs_review"           # flagged grounding; publishable with warnings
    FAILED_BLOCKED = "failed_blocked"       # repair budget exhausted; diagnostic-only
    CANCELLED = "cancelled"                 # honored external cancel
    BUDGET_EXCEEDED = "budget_exceeded"     # pre-call power-cut (RunBudget semantics)


TERMINALS: frozenset[RunState] = frozenset(
    {
        RunState.COMPLETED,
        RunState.NEEDS_REVIEW,
        RunState.FAILED_BLOCKED,
        RunState.CANCELLED,
        RunState.BUDGET_EXCEEDED,
    }
)

#: Only these two terminals may ever expose ``report.pdf`` upward.
PUBLISHABLE_STATES: frozenset[RunState] = frozenset(
    {RunState.COMPLETED, RunState.NEEDS_REVIEW}
)

_ACTIVE_ORDER: list[RunState] = [
    RunState.QUEUED,
    RunState.ENV_PREFLIGHT,
    RunState.EVIDENCE_PROVIDING,
    RunState.PLANNING,
    RunState.WRITING,
    RunState.AST_CONTRACT_QA,
    RunState.VISUAL_ENGINE,
    RunState.TYPST_COMPILING,
]

_STAGED: dict[RunState, set[RunState]] = {
    RunState.QUEUED: {RunState.ENV_PREFLIGHT},
    RunState.ENV_PREFLIGHT: {RunState.EVIDENCE_PROVIDING},
    RunState.EVIDENCE_PROVIDING: {RunState.PLANNING},
    RunState.PLANNING: {RunState.WRITING},
    RunState.WRITING: {RunState.AST_CONTRACT_QA},
    RunState.AST_CONTRACT_QA: {RunState.VISUAL_ENGINE, RunState.REPAIR_LOOP},
    RunState.VISUAL_ENGINE: {RunState.TYPST_COMPILING, RunState.REPAIR_LOOP},
    RunState.TYPST_COMPILING: {
        RunState.COMPLETED,
        RunState.NEEDS_REVIEW,
        RunState.REPAIR_LOOP,
    },
    # targeted patch applied → re-contract (block-level) or global recompile (asset);
    # repair budget exhausted → blocked
    RunState.REPAIR_LOOP: {
        RunState.AST_CONTRACT_QA,
        RunState.TYPST_COMPILING,
        RunState.FAILED_BLOCKED,
    },
}

# Every active state (including REPAIR_LOOP) can also be cut short by the two
# interrupts — and by FAILED_BLOCKED on a HARD fault (preflight missing a binary,
# a failing Typst compile, an unpatchable contract violation): the run must
# terminalize honestly, never hang or loop (docs/19 DoD terminal-state
# compliance). The repair-budget path (REPAIR_LOOP ⇒ FAILED_BLOCKED) stays;
# this widens only the fault source, never the terminal semantics.
for _s in _STAGED:
    _STAGED[_s] |= {RunState.CANCELLED, RunState.BUDGET_EXCEEDED, RunState.FAILED_BLOCKED}

TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    **{s: frozenset(t) for s, t in _STAGED.items()},
    **{s: frozenset() for s in TERMINALS},  # terminals are irreversible
}


class IllegalTransition(RuntimeError):
    """A state change the transition table forbids (terminal escape / stage skipping)."""


def validate_transition(current: RunState, target: RunState) -> None:
    """Central lifecycle check: raise unless ``current -> target`` is legal."""
    if target not in TRANSITIONS.get(current, frozenset()):
        raise IllegalTransition(
            f"illegal run transition: {current.value} -> {target.value}"
        )
