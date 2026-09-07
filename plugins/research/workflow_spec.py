"""Research's Workflow Definition: the structural spec the generic runtime consumes.

Research is ONE workflow on the generic core: this module declares its shape (stage
transition topology, the activities it may run, the cap dimensions bound on it) as
pure data. Runtime *values* — budgets, model/provider choices, worker counts — are
deliberately absent: changing them is configuration, not definition drift.

The module validates its own spec at import time: a malformed definition fails at
deploy, never mid-lease. The fingerprint minted by :meth:`ResearchWorkflow.fingerprint`
is stamped into the run checkpoint at ``begin_run`` and re-checked every turn — an
in-flight deploy that rewrites the flow terminalizes live runs as
``cause="definition_drift"`` instead of resuming them under a different flow.
"""
from __future__ import annotations

from workflow.definition import compute_fingerprint, validate_spec

# Mirrors plugin._LEGAL_NEXT; parity is asserted by
# tests/test_research_workflow_spec.py so the mirror cannot drift silently.
_STAGE_CHAIN: dict[str, str | None] = {
    "DISCOVER": "FRAME",
    "FRAME": "EVIDENCE",
    "EVIDENCE": "DESIGN",
    "DESIGN": "EXECUTE",
    "EXECUTE": "EXPLAIN",
    "EXPLAIN": "WRITE",
    "WRITE": "REVIEW",
    "REVIEW": "REPRODUCE",
    "REPRODUCE": "PUBLISH",
    "PUBLISH": None,  # terminal
}

# The one activity the auto-run flow executes; the agent kernel behind this logical
# id does all reasoning — the workflow only ever decides WHEN a turn runs, never HOW.
AUTO_TURN_TASK = "auto_turn"
RESEARCH_EXECUTOR_ID = "research-agent-kernel"


class ResearchWorkflow:
    """WorkflowDefinition-conformant object (duck-typed: ``name`` + ``spec()``)."""

    name = "research-auto-run"

    def spec(self) -> dict:
        return {
            "name": self.name,
            # PUBLISH is terminal: it appears only as a target, never a source.
            "transitions": {
                src: [dst] for src, dst in _STAGE_CHAIN.items() if dst is not None
            },
            "activities": [
                {"task_name": AUTO_TURN_TASK, "executor": RESEARCH_EXECUTOR_ID},
            ],
            # Declared dimensions only — ceilings are runtime config (see module doc).
            "caps": ["max_no_progress", "max_spend", "max_turns"],
            # No terminal hooks: research settle is adapter-side disk work the core
            # must never be handed as a generic hook (calibration: hooks:["settle"]
            # in a spec would smuggle research persistence into core semantics).
            "hooks": [],
        }

    def fingerprint(self) -> str:
        return compute_fingerprint(self.spec())


RESEARCH_WORKFLOW = ResearchWorkflow()

# Fail fast: the spec is checked at import/deploy, not at lease-acquire time.
validate_spec(RESEARCH_WORKFLOW.spec())
