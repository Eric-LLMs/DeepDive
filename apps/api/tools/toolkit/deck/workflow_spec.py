"""Slides' Workflow Definition: the structural spec the generic runtime consumes.

Mirrors ``plugins/research/workflow_spec.py`` one-for-one in shape (and nothing more):
Slides is ONE workflow on the generic core, declared as pure data — a stage
transition chain, the activities it may run, the cap dimensions bound on it. The
fingerprint is stamped into the deck run at start and re-checked every iteration: a
deploy that rewrites the flow mid-flight terminalizes live runs honestly instead of
resuming them under a different definition.

Runtime *values* (budgets, model/provider choices, concurrency) are deliberately
absent — changing them is configuration, not definition drift. The module validates
its own spec at import time: a malformed definition fails at deploy, never mid-run.
"""
from __future__ import annotations

from workflow.definition import compute_fingerprint, validate_spec

# The bounded cognition chain (directive §4): each stage is ONE workflow iteration.
# "brief_ready" is terminal: it appears only as a target, never a source.
_STAGE_CHAIN: dict[str, str | None] = {
    "TEXT_UNDERSTAND": "VISUAL_UNDERSTAND",
    "VISUAL_UNDERSTAND": "REDUCE",
    "REDUCE": "SYNTHESIZE",
    "SYNTHESIZE": None,  # terminal
}

# Activity names == stage vocabulary: one declared activity per stage, each bound to
# a LOGICAL executor id (a stable name, never a model/provider — see definition.py).
TEXT_UNDERSTAND_TASK = "understand_text"
VISUAL_UNDERSTAND_TASK = "understand_visual"
REDUCE_TASK = "reduce"
SYNTHESIZE_TASK = "synthesize"

TEXT_EXECUTOR_ID = "brief-text-understander"
VISUAL_EXECUTOR_ID = "brief-visual-understander"
REDUCE_EXECUTOR_ID = "brief-reducer"
SYNTH_EXECUTOR_ID = "brief-synthesizer"

# The fixed execution order the adapter's stage pointer walks, index 1..4.
STAGE_TASKS: list[tuple[str, str]] = [
    ("TEXT_UNDERSTAND", TEXT_UNDERSTAND_TASK),
    ("VISUAL_UNDERSTAND", VISUAL_UNDERSTAND_TASK),
    ("REDUCE", REDUCE_TASK),
    ("SYNTHESIZE", SYNTHESIZE_TASK),
]


class PresentationWorkflow:
    """WorkflowDefinition-conformant object (duck-typed: ``name`` + ``spec()``)."""

    name = "presentation-brief"

    def spec(self) -> dict:
        return {
            "name": self.name,
            "transitions": {
                src: [dst] for src, dst in _STAGE_CHAIN.items() if dst is not None
            },
            "activities": [
                {"task_name": TEXT_UNDERSTAND_TASK, "executor": TEXT_EXECUTOR_ID},
                {"task_name": VISUAL_UNDERSTAND_TASK, "executor": VISUAL_EXECUTOR_ID},
                {"task_name": REDUCE_TASK, "executor": REDUCE_EXECUTOR_ID},
                {"task_name": SYNTHESIZE_TASK, "executor": SYNTH_EXECUTOR_ID},
            ],
            # Declared dimensions only — ceilings are runtime config (see module doc).
            # max_turns is the honest runaway guard for a mispointed stage pointer;
            # max_no_progress brakes a stage that stops moving the deck forward.
            "caps": ["max_turns", "max_no_progress"],
            # No terminal hooks: brief/asset persistence is deck-side work the core
            # must never be handed as a generic hook (research's settle doctrine).
            "hooks": [],
        }

    def fingerprint(self) -> str:
        return compute_fingerprint(self.spec())


PRESENTATION_WORKFLOW = PresentationWorkflow()

# Fail fast: the spec is checked at import/deploy, not at iteration-acquire time.
validate_spec(PRESENTATION_WORKFLOW.spec())
