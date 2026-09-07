"""Workflow Core states: lifecycle table, terminal irreversibility, slot observation."""
from __future__ import annotations

import pytest

from workflow.states import (
    TRANSITIONS,
    IllegalTransition,
    WorkflowState,
    observe_state,
    validate_transition,
)

ALL_STATES = list(WorkflowState)
TERMINAL = {
    WorkflowState.WAITING,
    WorkflowState.SUCCEEDED,
    WorkflowState.FAILED,
    WorkflowState.CANCELLED,
}


class TestLifecycleTable:
    def test_terminal_predicate_partitions_the_enum(self):
        assert {s for s in ALL_STATES if s.is_terminal} == TERMINAL
        assert {s for s in ALL_STATES if not s.is_terminal} == {
            WorkflowState.IDLE, WorkflowState.RUNNING,
        }

    def test_every_state_has_a_transition_row(self):
        assert set(TRANSITIONS) == set(ALL_STATES)

    def test_only_idle_may_start(self):
        for source in ALL_STATES:
            assert (WorkflowState.RUNNING in TRANSITIONS[source]) == (
                source in (WorkflowState.IDLE, WorkflowState.RUNNING)
            )

    def test_terminal_states_have_no_way_out(self):
        for terminal in TERMINAL:
            assert TRANSITIONS[terminal] == set()
            for target in ALL_STATES:
                with pytest.raises(IllegalTransition):
                    validate_transition(terminal, target)

    def test_validate_accepts_every_listed_edge(self):
        for source, targets in TRANSITIONS.items():
            for target in ALL_STATES:
                if target in targets:
                    validate_transition(source, target)  # must not raise
                else:
                    with pytest.raises(IllegalTransition, match=source.value):
                        validate_transition(source, target)

    def test_continue_edge_is_running_to_running(self):
        validate_transition(WorkflowState.RUNNING, WorkflowState.RUNNING)

    def test_idle_may_only_start(self):
        with pytest.raises(IllegalTransition):
            validate_transition(WorkflowState.IDLE, WorkflowState.SUCCEEDED)
        with pytest.raises(IllegalTransition):
            validate_transition(WorkflowState.IDLE, WorkflowState.WAITING)


class TestObserveState:
    def test_matching_running_slot_is_observed_as_running(self):
        active = {"run_id": "r1", "status": "RUNNING"}
        assert observe_state(active, "r1") is WorkflowState.RUNNING

    def test_foreign_run_observes_idle(self):
        active = {"run_id": "other", "status": "RUNNING"}
        assert observe_state(active, "r1") is WorkflowState.IDLE

    @pytest.mark.parametrize("active", [None, {}, {"run_id": "r1"}, {"status": "DONE"}])
    def test_missing_or_unrunning_slot_observes_idle(self, active):
        assert observe_state(active, "r1") is WorkflowState.IDLE
