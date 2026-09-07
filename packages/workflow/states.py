"""Workflow lifecycle states: the minimal, domain-free execution state machine.

The six states below are the complete vocabulary a generic workflow needs to answer one
question about a live execution: *where is it in its lifecycle?* Everything business-shaped
is deliberately absent — those concerns are *inputs* to the loop policy
(:mod:`workflow.policy`), encoded as :class:`Grade` causes, never as states.

Terminal states are irreversible. ``WAITING`` means exactly one thing: the execution ended
because it is *waiting on an external condition* (a signal, a human decision, a timer) —
never as a euphemism for a policy-detected failure (see ``validate_transition`` and the
grade table in ``policy.py``).
"""
from __future__ import annotations

import enum


class WorkflowState(str, enum.Enum):
    IDLE = "idle"            # no live execution owns the slot
    RUNNING = "running"      # an execution owns the slot and is advancing
    WAITING = "waiting"      # terminal: parked on an external condition (signal / human / timer)
    SUCCEEDED = "succeeded"  # terminal: the definition's success predicate fired
    FAILED = "failed"        # terminal: policy-detected inability to continue, or an error
    CANCELLED = "cancelled"  # terminal: an external cancel was observed and honored

    @property
    def is_terminal(self) -> bool:
        return self in {
            WorkflowState.WAITING,
            WorkflowState.SUCCEEDED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }


# Legal transitions: only IDLE may become RUNNING; a terminal state has no way out. The
# runner never transitions *to* IDLE explicitly — releasing the slot *is* the execution
# returning to IDLE on disk — so every in-code transition is RUNNING->RUNNING (continue)
# or RUNNING->terminal (stop). Anything else is a bug and is refused loudly.
TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.IDLE: {WorkflowState.RUNNING},
    WorkflowState.RUNNING: {
        WorkflowState.RUNNING,
        WorkflowState.WAITING,
        WorkflowState.SUCCEEDED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.WAITING: set(),
    WorkflowState.SUCCEEDED: set(),
    WorkflowState.FAILED: set(),
    WorkflowState.CANCELLED: set(),
}


class IllegalTransition(RuntimeError):
    """A lifecycle change the transition table forbids (terminal escape / non-IDLE start)."""


def validate_transition(current: WorkflowState, target: WorkflowState) -> None:
    """Central lifecycle check: raise unless ``current -> target`` is legal."""
    if target not in TRANSITIONS.get(current, set()):
        raise IllegalTransition(
            f"illegal workflow transition: {current.value} -> {target.value}"
        )


def observe_state(active: dict | None, run_id: str) -> WorkflowState:
    """Derive the state an arriving job should see from the persisted slot record.

    ``RUNNING`` iff the slot is held by *this* ``run_id`` and marked running. A terminal
    outcome has already released the slot, so it shows up as ``IDLE`` — callers then drop
    the job instead of resurrecting a finished execution. ``slot_key``/``status`` spellings
    belong to the adapter that built the dict; here they are the neutral lease vocabulary.
    """
    if active and active.get("run_id") == run_id and active.get("status") == "RUNNING":
        return WorkflowState.RUNNING
    return WorkflowState.IDLE
