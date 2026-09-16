"""The Slides adapter: drives the generic core once per stage, in-process, to terminal.

Same boundary as Research OS — the core owns *how one iteration executes*
(:func:`workflow.runner.drive_iteration`: lease contest, heartbeat, grading, settle);
this module owns *which activity runs next and what the facts mean*. The difference is
purely scheduling: a deck brief is a bounded four-stage chain that completes inside the
Toolkit worker, so instead of enqueueing the next job this adapter loops the stage
pointer (index 1..4 → :data:`.workflow_spec.STAGE_TASKS`) until the outcome is
terminal or dropped. No queue vocabulary enters here; no core abstraction is added.

Outcome translation follows the research doctrine verbatim: a non-transient executor
failure re-raises its ORIGINAL cause (loud, never a laundered error string); a dropped
lease or an ungradeable terminal stop raises a typed adapter exception; only a
SUCCEEDED grade carrying a materialized brief is a result.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from workflow.leases import LeaseConfig
from workflow.retry import RetryPolicy
from workflow.runner import (
    ExecutionFailed,
    IterationOutcome,
    IterationRequest,
    RunCounters,
    drive_iteration,
)
from workflow.runtime import MappingRegistry, build_deps
from workflow.states import WorkflowState

from .schema import (
    DocumentRepresentation,
    PresentationBrief,
    PresentationControls,
    PresentationWorkflowConfig,
)
from .workflow_executors import (
    DeckRunContext,
    ReduceExecutor,
    SynthesizeExecutor,
    TextUnderstandExecutor,
    VisualUnderstandExecutor,
)
from .workflow_spec import (
    PRESENTATION_WORKFLOW,
    REDUCE_EXECUTOR_ID,
    STAGE_TASKS,
    SYNTH_EXECUTOR_ID,
    TEXT_EXECUTOR_ID,
    VISUAL_EXECUTOR_ID,
)
from .workflow_store import DeckLeaseStore


class DeckWorkflowError(RuntimeError):
    """Base for adapter-side terminal translations (never a silent degrade)."""


class DeckWorkflowCancelled(DeckWorkflowError):
    """The execution honored an external stop (host cancel or ledger flag)."""


class DeckWorkflowWaiting(DeckWorkflowError):
    """Parked on an external condition. A bounded in-process brief cannot wait —
    the channel is declared and graded, but reaching it is a stop the caller must
    surface, not swallow."""


class DeckWorkflowStopped(DeckWorkflowError):
    """A terminal stop that is neither success nor cancel (cap, structural, drop)."""

    def __init__(self, message: str, outcome: IterationOutcome | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome


class DeckProgressProbe:
    """"Did this iteration move the deck forward?" — domain facts, not lease state."""

    def __init__(self, ctx: DeckRunContext) -> None:
        self._ctx = ctx

    def snapshot(self) -> dict:
        ctx = self._ctx
        return {
            "sections": len(ctx.sections),
            "visuals": len(ctx.visuals),
            "has_model": ctx.model is not None,
            "has_brief": ctx.brief is not None,
        }

    def changed(self, before: dict, after: dict) -> bool:
        return before != after


async def run_brief_workflow(
    *,
    llm: Any,
    doc_rep: DocumentRepresentation,
    controls: PresentationControls,
    deck_id: str,
    config: PresentationWorkflowConfig | None = None,
    cancel: Callable[[], bool] | None = None,
    pending_signals: Callable[[], int] | None = None,
    lease_store: DeckLeaseStore | None = None,
) -> tuple[PresentationBrief, dict]:
    """Run TEXT_UNDERSTAND → VISUAL_UNDERSTAND → REDUCE → SYNTHESIZE to terminal.

    Returns ``(brief, stats)``; raises the executor's own error type (e.g.
    :class:`~tools.toolkit.errors.GenerationError`) on a loud stage failure, or a
    :class:`DeckWorkflowError` subclass on a non-success terminal. ``lease_store`` is
    an optional host seam (stop button / observability); absent, the run owns one.
    """
    ctx = DeckRunContext(
        deck_id=deck_id, llm=llm, doc_rep=doc_rep, controls=controls,
        config=config or PresentationWorkflowConfig(),
    )
    store = lease_store or DeckLeaseStore()       # fresh ledger: per-run isolation
    registry = MappingRegistry({
        TEXT_EXECUTOR_ID: TextUnderstandExecutor(ctx),
        VISUAL_EXECUTOR_ID: VisualUnderstandExecutor(ctx),
        REDUCE_EXECUTOR_ID: ReduceExecutor(ctx),
        SYNTH_EXECUTOR_ID: SynthesizeExecutor(ctx),
    })
    probe = DeckProgressProbe(ctx)
    # Lease intervals far beyond any sane stage duration: a brief run is one process,
    # so "stale owner" has no meaning; cancel is still observed via the ledger flag
    # (business facts / next acquire) — only the reclaim machinery is neutralized.
    lease = LeaseConfig(refresh_s=3600.0, stale_s=86400.0)
    run_id = f"deck-{deck_id}"
    fingerprint = PRESENTATION_WORKFLOW.fingerprint()

    counters = RunCounters()
    index = 1
    while True:
        if index > len(STAGE_TASKS):
            # Defensive: the pointer may not run off the declared chain. If SYNTHESIZE
            # graded continue without a brief, that is a bug, not a fifth stage.
            raise DeckWorkflowStopped(
                f"stage pointer ran off the declared chain at index {index}")
        stage, task = STAGE_TASKS[index - 1]
        deps = build_deps(
            definition=PRESENTATION_WORKFLOW,
            registry=registry,
            store=store,
            probe=probe,
            business_facts=lambda: {
                "finished": ctx.brief is not None,
                "pending_signals": pending_signals() if pending_signals else 0,
                "cancel_requested": bool(cancel and cancel()),
            },
            # opaque to the core; the executor carries its facts on the ctx
            compose_prompt=lambda req, attempt, _t=task, _s=stage: (
                f"{_t} stage={_s} attempt={attempt}"),
            retry=RetryPolicy(),                    # corrective retries live in the engine
            task_name=task,
            caps_values={
                "max_turns": ctx.config.brief_max_turns,
                "max_no_progress": ctx.config.brief_max_no_progress,
            },
            lease=lease,
        )
        try:
            outcome = await drive_iteration(deps, IterationRequest(
                run_id=run_id, index=index, counters=counters))
        except ExecutionFailed as ef:
            raise ef.cause from ef                  # loud, truthful original error

        if outcome.dropped:
            raise DeckWorkflowStopped(
                f"iteration {index} ({stage}) dropped: {outcome.reason}", outcome)
        if outcome.state is WorkflowState.CANCELLED:
            raise DeckWorkflowCancelled(
                outcome.reason or "presentation brief cancelled")
        if outcome.state is WorkflowState.WAITING:
            raise DeckWorkflowWaiting(
                f"iteration {index} ({stage}) parked: {outcome.reason}")
        if outcome.action == "continue":
            counters = outcome.counters
            index = outcome.next_index or (index + 1)
            continue
        if outcome.state is WorkflowState.SUCCEEDED:
            if ctx.brief is None:
                raise DeckWorkflowStopped(
                    f"{fingerprint}: graded SUCCEEDED with no brief materialized",
                    outcome)
            return ctx.brief, ctx.stats
        raise DeckWorkflowStopped(
            f"iteration {index} ({stage}) stopped: {outcome.reason} "
            f"(cause={outcome.cause})", outcome)
