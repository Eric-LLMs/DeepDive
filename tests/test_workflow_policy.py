"""Loop policy chain: priority order, terminal vocabulary, meters, caps."""
from __future__ import annotations

import pytest

from workflow.policy import (
    CAP_REASON,
    CAUSE_CANCEL,
    CAUSE_FINISHED,
    CAUSE_NO_PROGRESS,
    CAUSE_PENDING_SIGNAL,
    CAUSE_SPEND_CAP,
    CAUSE_TURN_CAP,
    IterationFacts,
    LoopCaps,
    LoopPolicy,
)
from workflow.states import WorkflowState


def _facts(**kw) -> IterationFacts:
    base = dict(index=1, total_spend=0.0)
    base.update(kw)
    return IterationFacts(**base)


class TestPriorityChain:
    def test_cancel_beats_everything(self):
        p = LoopPolicy(caps=LoopCaps(max_turns=1))
        g = p.grade(_facts(
            cancel_requested=True, finished=True, pending_signals=3,
            progress=False, consecutive_no_progress=9, index=1,
        ))
        assert (g.state, g.cause) == (WorkflowState.CANCELLED, CAUSE_CANCEL)

    def test_finished_beats_signal_and_brakes(self):
        p = LoopPolicy(caps=LoopCaps(max_turns=1, max_no_progress=1))
        g = p.grade(_facts(finished=True, pending_signals=2, progress=False,
                           consecutive_no_progress=5, index=99))
        assert (g.state, g.cause) == (WorkflowState.SUCCEEDED, CAUSE_FINISHED)

    def test_pending_signal_beats_no_progress_and_caps(self):
        p = LoopPolicy(caps=LoopCaps(max_turns=1, max_no_progress=1, max_spend=0.0))
        g = p.grade(_facts(pending_signals=1, progress=False,
                           consecutive_no_progress=4, index=50, total_spend=99.0))
        assert (g.state, g.cause) == (WorkflowState.WAITING, CAUSE_PENDING_SIGNAL)

    def test_no_progress_beats_caps(self):
        p = LoopPolicy(caps=LoopCaps(max_no_progress=2, max_turns=5))
        g = p.grade(_facts(progress=False, consecutive_no_progress=1, index=5))
        assert (g.state, g.cause) == (WorkflowState.FAILED, CAUSE_NO_PROGRESS)

    def test_turn_cap_beats_spend_cap(self):
        p = LoopPolicy(caps=LoopCaps(max_turns=5, max_spend=1.0))
        g = p.grade(_facts(progress=True, index=5, total_spend=2.0))
        assert (g.state, g.cause) == (WorkflowState.FAILED, CAUSE_TURN_CAP)
        assert g.reason == CAP_REASON

    def test_continue_when_nothing_fires(self):
        p = LoopPolicy(caps=LoopCaps(max_turns=8, max_no_progress=2, max_spend=10.0))
        g = p.grade(_facts(progress=True, index=3, total_spend=1.0))
        assert g.state is None and g.cause is None
        assert g.consecutive_no_progress == 0  # progress resets the brake


class TestTerminalVocabulary:
    """Calibration #1: caps are policy stops by DEFAULT; WAITING is an adapter declaration."""

    def test_cap_defaults_to_failed_not_waiting(self):
        g = LoopPolicy(caps=LoopCaps(max_turns=2)).grade(_facts(index=2, progress=True))
        assert (g.state, g.cause, g.reason) == (
            WorkflowState.FAILED, CAUSE_TURN_CAP, CAP_REASON,
        )

    def test_adapter_can_declare_caps_as_external_wait(self):
        # "hitting a cap parks this workflow for a human signal" — the declaration is a
        # constructor choice, never an enum of business outcomes.
        p = LoopPolicy(caps=LoopCaps(max_spend=1.0), cap_outcome=WorkflowState.WAITING)
        g = p.grade(_facts(index=1, total_spend=1.0, progress=True))
        assert (g.state, g.cause) == (WorkflowState.WAITING, CAUSE_SPEND_CAP)
        assert g.reason == CAP_REASON

    def test_cap_outcome_must_be_a_legal_terminal_choice(self):
        with pytest.raises(ValueError):
            LoopPolicy(cap_outcome=WorkflowState.RUNNING)

    def test_no_progress_is_always_a_failed_brake(self):
        p = LoopPolicy(caps=LoopCaps(max_no_progress=1), cap_outcome=WorkflowState.WAITING)
        g = p.grade(_facts(progress=False))
        assert (g.state, g.cause) == (WorkflowState.FAILED, CAUSE_NO_PROGRESS)

    def test_finished_predicate_is_a_provided_fact_not_a_state_name(self):
        # The core never learns what "done" means in business terms — it trusts the fact.
        p = LoopPolicy()
        assert p.grade(_facts(finished=True)).state is WorkflowState.SUCCEEDED


class TestCounterSemantics:
    def test_consecutive_accumulates_across_grades(self):
        p = LoopPolicy(caps=LoopCaps(max_no_progress=4))
        g1 = p.grade(_facts(progress=False, consecutive_no_progress=0))
        g2 = p.grade(_facts(progress=False, consecutive_no_progress=g1.consecutive_no_progress))
        g3 = p.grade(_facts(progress=False,
                            consecutive_no_progress=g2.consecutive_no_progress))
        assert [g1.consecutive_no_progress, g2.consecutive_no_progress,
                g3.consecutive_no_progress] == [1, 2, 3]
        assert g3.state is None  # cap of 4 not yet reached
        g4 = p.grade(_facts(progress=False, consecutive_no_progress=3))
        assert (g4.state, g4.cause) == (WorkflowState.FAILED, CAUSE_NO_PROGRESS)

    def test_turn_cap_is_inclusive_at_the_ceiling(self):
        p = LoopPolicy(caps=LoopCaps(max_turns=8))
        assert p.grade(_facts(index=7, progress=True)).state is None
        assert p.grade(_facts(index=8, progress=True)).cause == CAUSE_TURN_CAP

    def test_absent_caps_disable_the_dimension(self):
        p = LoopPolicy(caps=LoopCaps())
        g = p.grade(_facts(index=10_000, total_spend=1e9, progress=False,
                           consecutive_no_progress=999))
        assert g.state is None  # only the (uncapped) brake would have fired; it is off too

    def test_unknown_spend_never_trips_the_spend_cap(self):
        p = LoopPolicy(caps=LoopCaps(max_spend=1.0))
        g = p.grade(_facts(total_spend=None, progress=True, index=1))
        assert g.state is None  # meter unknown: cannot compare, do not fabricate a trigger

    def test_spend_cap_is_inclusive_at_the_ceiling(self):
        p = LoopPolicy(caps=LoopCaps(max_spend=1.0))
        assert p.grade(_facts(total_spend=0.999, progress=True)).state is None
        assert p.grade(_facts(total_spend=1.0, progress=True)).cause == CAUSE_SPEND_CAP
