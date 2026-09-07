"""Loop policy: the fixed-priority stop/continue decision over one completed iteration.

The *machine* (chain order + counters) is generic; the *predicates* (what counts as done,
which signals are pending, what progress looks like) are computed by the adapter and
handed in as data (:class:`IterationFacts`). Priority chain, most-significant first:

1. ``cancel``     — an external stop always wins, over every other consideration.
2. ``finished``   — the definition's own success predicate fired (adapter's call).
3. ``signal``     — the execution is parked on an external condition (WAITING).
4. ``no-progress``— the brake: consecutive iterations without a visible milestone.
5. ``turn cap`` / ``spend cap`` — policy-detected inability to continue.

Calibration #1 (terminal vocabulary): a cap firing is NOT intrinsically "waiting" — it is
a policy-detected inability to continue, so the generic outcome is
``FAILED(reason="budget_or_turn_cap_exceeded")``. Only when the deployment *declares*
("hitting a cap parks this workflow for a human signal") does the outcome become WAITING;
that declaration lives in the adapter's constructor argument, never in the state enum.
The no-progress brake likewise yields ``FAILED``; a workflow that wants a different
discriminant for the UI reads :attr:`Grade.cause` — the stable machine code — instead of
pattern-matching free text.

``spend`` is a single accumulated meter with an explicit unknown state (``None``):
metered-but-unpriced work is never laundered into a ``0.0`` total.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from workflow.states import WorkflowState

# Stable discriminants for adapter-side mapping (UI kinds, metrics). Free-text ``reason``
# is for humans; ``cause`` is for code.
CAUSE_CANCEL = "cancel"
CAUSE_FINISHED = "finished"
CAUSE_PENDING_SIGNAL = "pending_signal"
CAUSE_NO_PROGRESS = "no_progress"
CAUSE_TURN_CAP = "turn_cap_exceeded"
CAUSE_SPEND_CAP = "spend_cap_exceeded"

CAP_REASON = "budget_or_turn_cap_exceeded"


@dataclasses.dataclass(frozen=True)
class LoopCaps:
    """Optional ceilings; ``None`` disables that dimension. Values are runtime config —
    they ride in the request, deliberately NOT in the definition spec."""

    max_turns: int | None = None
    max_no_progress: int | None = None
    max_spend: float | None = None


@dataclasses.dataclass(frozen=True)
class IterationFacts:
    """Everything the chain needs, pre-computed by the adapter from the *real* state."""

    finished: bool = False
    pending_signals: int = 0
    cancel_requested: bool = False
    progress: bool = False
    consecutive_no_progress: int = 0
    index: int = 1
    total_spend: float | None = 0.0


@dataclasses.dataclass(frozen=True)
class Grade:
    state: WorkflowState | None          # terminal outcome, or None -> continue
    cause: str | None = None
    reason: str | None = None
    consecutive_no_progress: int = 0


@runtime_checkable
class ProgressProbe(Protocol):
    """Adapter seam answering "did this iteration move anything a user can see?".

    The adapter owns what counts (its own fingerprint), the core owns when it is asked:
    before/after every iteration execution.
    """

    async def snapshot(self) -> Mapping[str, Any]: ...

    def changed(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> bool: ...


class LoopPolicy:
    """The priority chain. Constructed once per run; stateless across ``grade`` calls."""

    def __init__(
        self,
        *,
        caps: LoopCaps = LoopCaps(),
        cap_outcome: WorkflowState = WorkflowState.FAILED,
        signal_outcome: WorkflowState = WorkflowState.WAITING,
    ) -> None:
        if cap_outcome not in (WorkflowState.FAILED, WorkflowState.WAITING):
            raise ValueError("cap_outcome must be FAILED (policy stop) or WAITING (parked for a signal)")
        self.caps = caps
        self.cap_outcome = cap_outcome
        self.signal_outcome = signal_outcome

    def grade(self, facts: IterationFacts) -> Grade:
        consecutive = facts.consecutive_no_progress
        if facts.cancel_requested:
            return Grade(WorkflowState.CANCELLED, CAUSE_CANCEL,
                         "stop requested by the user", consecutive)
        if facts.finished:
            return Grade(WorkflowState.SUCCEEDED, CAUSE_FINISHED,
                         "the definition's completion predicate is satisfied", consecutive)
        if facts.pending_signals:
            return Grade(self.signal_outcome, CAUSE_PENDING_SIGNAL,
                         f"{facts.pending_signals} external signal(s) awaiting a decision",
                         consecutive)
        new_consecutive = consecutive + 1 if not facts.progress else 0
        if self.caps.max_no_progress and new_consecutive >= self.caps.max_no_progress:
            return Grade(WorkflowState.FAILED, CAUSE_NO_PROGRESS,
                         f"no visible progress across {new_consecutive} consecutive "
                         f"iterations", new_consecutive)
        if self.caps.max_turns is not None and facts.index >= self.caps.max_turns:
            return Grade(self.cap_outcome, CAUSE_TURN_CAP, CAP_REASON, new_consecutive)
        if (
            self.caps.max_spend is not None
            and facts.total_spend is not None
            and facts.total_spend >= self.caps.max_spend
        ):
            return Grade(self.cap_outcome, CAUSE_SPEND_CAP, CAP_REASON, new_consecutive)
        return Grade(None, None, None, new_consecutive)
