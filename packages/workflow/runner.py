"""The runner: one generic iteration of a workflow execution, start to settle.

``drive_iteration`` is the choreography a workflow driver grows into — acquire the lease,
execute the task behind a heartbeat + cooperative cancel, bisect failures through the
retry policy, probe progress, grade with the loop policy, offer the terminal hook its
last look, and record the outcome. It deliberately owns NO I/O, NO scheduling and NO
business vocabulary: every collaborator arrives through :mod:`workflow.ports`, and the
two facts only an adapter can know (``finished`` / ``pending_signals``) are recomputed
per grading through the injected ``business_facts`` callable against the fresh state.

Two rules keep the shape honest:

- **The slot outlives nothing it cannot explain.** Every exit path either leaves a
  settled ledger (continue) or settles it; the *caller* (adapter) converts a settle into
  slot release and owns enqueueing the next iteration (``next_index`` on the outcome).
- **A non-transient executor failure is terminal but loud**: the ledger is settled, then
  the original exception is re-raised (wrapped) so the delivery layer's job row fails
  truthfully.

Metering rule at core level: an UNKNOWN spend (``None``) is counted separately and never
laundered into the numeric total; the numeric total is what the spend cap compares.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from workflow.leases import (
    STATE_RUNNING,
    LeaseConfig,
    LeaseDecision,
    LeaseLedger,
    acquire,
    mark_done,
    renew,
)
from workflow.policy import CAUSE_CANCEL, Grade, IterationFacts, LoopPolicy
from workflow.ports import (
    EventPublisher,
    Executor,
    LeaseStore,
    NoopPublisher,
    TaskRequest,
    TerminalHook,
)
from workflow.retry import RetryPolicy
from workflow.states import WorkflowState, validate_transition


@dataclasses.dataclass(frozen=True)
class RunCounters:
    """Loop bookkeeping the adapter persists in its own checkpoint and hands in fresh."""

    consecutive_no_progress: int = 0
    total_spend: float = 0.0
    unknown_spend_count: int = 0

    def absorb_spend(self, spend: float | None) -> RunCounters:
        if spend is None:  # UNKNOWN: counted, never laundered into the numeric total
            return dataclasses.replace(self, unknown_spend_count=self.unknown_spend_count + 1)
        return dataclasses.replace(self, total_spend=self.total_spend + spend)


@dataclasses.dataclass(frozen=True)
class IterationRequest:
    run_id: str
    index: int                        # 1-based iteration this job acquires
    counters: RunCounters = RunCounters()


@dataclasses.dataclass(frozen=True)
class IterationOutcome:
    state: WorkflowState
    action: str                       # continue | <terminal-state-value> | dropped
    dropped: bool = False
    reason: str | None = None
    cause: str | None = None
    run_id: str | None = None
    index: int = 0
    attempt: int = 1
    execution_id: str | None = None
    progress: bool = False
    counters: RunCounters = RunCounters()
    consecutive_no_progress: int = 0
    value: str | None = None          # executor output when a run actually happened
    next_index: int | None = None     # set only when action == "continue"


class ExecutionFailed(RuntimeError):
    """Non-transient executor failure: outcome attached, original cause in ``cause``."""

    def __init__(self, outcome: IterationOutcome, cause: BaseException) -> None:
        super().__init__(f"execution failed: {cause}")
        self.outcome = outcome
        self.cause = cause


class RunnerDeps:
    """Everything injectable, nothing assumed."""

    def __init__(
        self,
        *,
        store: LeaseStore,
        executor: Executor,
        policy: LoopPolicy,
        probe: Any,                     # ProgressProbe (snapshot may be sync or async)
        retry: RetryPolicy,
        business_facts: Callable[[], Mapping[str, Any]]
        | Callable[[], Awaitable[Mapping[str, Any]]],
        compose_prompt: Callable[[IterationRequest, int], str],  # (request, attempt)
        lease: LeaseConfig = LeaseConfig(),
        publisher: EventPublisher | None = None,
        hook: TerminalHook | None = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.policy = policy
        self.probe = probe
        self.retry = retry
        self.business_facts = business_facts
        self.compose_prompt = compose_prompt
        self.lease = lease
        self.publisher = publisher or NoopPublisher()
        self.hook = hook


async def drive_iteration(deps: RunnerDeps, req: IterationRequest) -> IterationOutcome:
    # ── 1. lease contest (one atomic section) ─────────────────────────────────
    box: dict[str, LeaseDecision] = {}

    def _contest(ledger: LeaseLedger) -> LeaseLedger:
        box["decision"] = acquire(
            ledger, run_id=req.run_id, index=req.index,
            config=deps.lease, now_iso=_now_iso(), now_epoch=_now_epoch(),
        )
        return box["decision"].ledger

    deps.store.atomic(_contest)
    decision = box["decision"]
    if decision.action == "dropped":
        return IterationOutcome(
            state=WorkflowState.RUNNING, action="dropped", dropped=True,
            reason=decision.reason, run_id=req.run_id, index=req.index,
            counters=req.counters,
        )
    if decision.action == "cancelled":
        validate_transition(WorkflowState.RUNNING, WorkflowState.CANCELLED)
        return IterationOutcome(
            state=WorkflowState.CANCELLED, action=WorkflowState.CANCELLED.value,
            reason=decision.reason, cause=CAUSE_CANCEL,
            run_id=req.run_id, index=req.index,
            execution_id=decision.identity.as_str() if decision.identity else None,
            counters=req.counters,
        )

    attempt = decision.identity.attempt if decision.identity else 1
    execution_id = decision.identity.as_str() if decision.identity else None
    counters = req.counters

    # ── 2. execute the task: retries for transient faults, cancel-aware ──────
    before = await _snapshot(deps)
    value: str | None = None
    interrupted_by_cancel = False
    while True:
        result, error, was_cancelled = await _execute_once(deps, req, attempt, execution_id)
        if error is not None:
            transient = deps.retry.is_transient(error)
            if deps.retry.should_retry(error, attempt):
                attempt += 1
                prev_execution = execution_id
                execution_id = f"{req.run_id}:{req.index}:{attempt}"

                def _rearm(
                    ledger: LeaseLedger, _prev: str = prev_execution,
                    _new: str = execution_id, _attempt: int = attempt,
                ) -> LeaseLedger:
                    if ledger.execution_id != _prev:
                        return ledger  # fenced: ownership moved while we were down
                    return dataclasses.replace(
                        ledger, attempt=_attempt, state=STATE_RUNNING,
                        execution_id=_new, updated_at=_now_iso(),
                    )

                deps.store.atomic(_rearm)
                if _ownership_lost(deps, execution_id):
                    return _dropped(
                        req, counters,
                        "ownership lost (reclaimed by a successor) during retry re-arm",
                    )
                await asyncio.sleep(deps.retry.wait_s(attempt))
                continue
            # Retries exhausted OR non-transient: settle the ledger either way.
            deps.store.atomic(
                lambda ledger: mark_done(
                    ledger, now_iso=_now_iso(), owner_execution=execution_id
                )
            )
            if _ownership_lost(deps, execution_id):
                # A zombie's failure outcome must not terminalize a live successor's run.
                return _dropped(
                    req, counters,
                    f"ownership lost while settling failed iteration: {error}",
                )
            reason = (
                f"transient failures exhausted after {attempt} attempt(s): {error}"
                if transient else f"iteration failed: {error}"
            )
            outcome = IterationOutcome(
                state=WorkflowState.FAILED, action=WorkflowState.FAILED.value,
                reason=reason, cause="transient_exhausted" if transient else "error",
                run_id=req.run_id, index=req.index, attempt=attempt,
                execution_id=execution_id, counters=counters,
            )
            if transient:
                return outcome
            raise ExecutionFailed(outcome, error)
        if was_cancelled:
            interrupted_by_cancel = True
            break
        if result is not None:
            value = result.value
            counters = counters.absorb_spend(result.spend)
        break

    # ── 3. grade with fresh authoritative facts ───────────────────────────────
    after = await _snapshot(deps)
    progress = bool(deps.probe.changed(before, after))
    ledger = deps.store.read()
    facts = IterationFacts(
        progress=progress,
        consecutive_no_progress=counters.consecutive_no_progress,
        index=req.index,
        total_spend=counters.total_spend,
        **await _business_facts(deps),
    )
    facts = dataclasses.replace(
        facts,
        cancel_requested=bool(facts.cancel_requested) or bool(ledger.cancel_requested)
        or interrupted_by_cancel,
    )
    grade = deps.policy.grade(facts)
    counters = dataclasses.replace(
        counters, consecutive_no_progress=grade.consecutive_no_progress
    )
    if interrupted_by_cancel and grade.state is None:
        grade = Grade(WorkflowState.CANCELLED, CAUSE_CANCEL,
                      "stop requested by the user", counters.consecutive_no_progress)

    # ── 4. settle: continue marker or terminal outcome, hook gets the last look ─
    if grade.state is None:
        deps.store.atomic(
            lambda ledger: mark_done(
                ledger, now_iso=_now_iso(), owner_execution=execution_id
            )
        )
        if _ownership_lost(deps, execution_id):
            return _dropped(
                req, counters, "ownership lost during settle (successor reclaimed)"
            )
        await deps.publisher.publish(kind="continue", revision=None)
        return IterationOutcome(
            state=WorkflowState.RUNNING, action="continue",
            run_id=req.run_id, index=req.index, attempt=attempt,
            execution_id=execution_id, progress=progress, counters=counters,
            consecutive_no_progress=counters.consecutive_no_progress,
            value=value, next_index=req.index + 1,
        )

    if deps.hook is not None:
        rewritten = await deps.hook.before_terminalize(
            grade,
            {
                "index": req.index, "attempt": attempt, "progress": progress,
                "value": value, "execution_id": execution_id,
            },
        )
        if rewritten is not None:
            grade = rewritten
        if grade.state is None:  # the hook turned the stop back into a continuation
            deps.store.atomic(
                lambda ledger: mark_done(
                    ledger, now_iso=_now_iso(), owner_execution=execution_id
                )
            )
            if _ownership_lost(deps, execution_id):
                return _dropped(
                    req, counters, "ownership lost during hook-rewritten settle"
                )
            return IterationOutcome(
                state=WorkflowState.RUNNING, action="continue",
                run_id=req.run_id, index=req.index, attempt=attempt,
                execution_id=execution_id, progress=progress, counters=counters,
                consecutive_no_progress=counters.consecutive_no_progress,
                value=value, next_index=req.index + 1,
            )

    validate_transition(WorkflowState.RUNNING, grade.state)
    deps.store.atomic(
        lambda ledger: mark_done(
            ledger, now_iso=_now_iso(), owner_execution=execution_id
        )
    )
    if _ownership_lost(deps, execution_id):
        # The settle above was fenced: our outcome is stale — the successor owns the
        # fate. Do NOT publish, do NOT return a terminal decision.
        return _dropped(
            req, counters, "ownership lost before terminal settle (decision discarded)"
        )
    await deps.publisher.publish(kind=grade.state.value, revision=None)
    return IterationOutcome(
        state=grade.state, action=grade.state.value,
        reason=grade.reason, cause=grade.cause,
        run_id=req.run_id, index=req.index, attempt=attempt,
        execution_id=execution_id, progress=progress, counters=counters,
        consecutive_no_progress=counters.consecutive_no_progress, value=value,
    )


# ── one attempt: heartbeat + cooperative cancel around the black box ─────────
async def _execute_once(
    deps: RunnerDeps, req: IterationRequest, attempt: int, execution_id: str | None = None
):
    cancel_event = asyncio.Event()
    stop = asyncio.Event()
    watcher = asyncio.create_task(
        _lease_watcher(deps, req, stop, cancel_event, execution_id)
    )
    prompt = deps.compose_prompt(req, attempt)
    run_task = asyncio.create_task(deps.executor.execute(TaskRequest(prompt=prompt)))
    cancel_waiter = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            {run_task, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        stop.set()
        watcher.cancel()
        if not cancel_waiter.done():
            cancel_waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_waiter

    if run_task in done:
        try:
            return await run_task, None, False
        except asyncio.CancelledError:
            return None, None, True
        except Exception as exc:  # noqa: BLE001 — classified by the retry policy
            return None, exc, False
    run_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run_task
    return None, None, True


async def _lease_watcher(
    deps: RunnerDeps, req: IterationRequest, stop: asyncio.Event,
    cancel_event: asyncio.Event, execution_id: str | None = None,
) -> None:
    """Heartbeat the lease; flag an external cancel; stand down when ownership moved."""
    while not stop.is_set():
        try:
            await asyncio.sleep(deps.lease.refresh_s)
        except asyncio.CancelledError:
            return
        try:
            ledger = deps.store.read()
        except Exception:  # noqa: BLE001 — watcher is advisory; keep the iteration running
            continue
        if ledger.run_id not in (None, req.run_id) or ledger.index != req.index:
            return  # ownership moved (new run / next iteration) — stop touching the ledger
        if execution_id is not None and ledger.execution_id != execution_id:
            return  # fenced: a successor reclaimed this iteration (attempt moved on)
        if ledger.cancel_requested:
            cancel_event.set()
            return
        deps.store.atomic(
            lambda ledger: renew(
                ledger, now_iso=_now_iso(), owner_execution=execution_id
            )
        )


# ── F2 fencing helpers ────────────────────────────────────────────────────────
def _ownership_lost(deps: RunnerDeps, execution_id: str | None) -> bool:
    """True once the on-disk lease no longer names our execution as its holder.

    Read failures never fabricate a loss: only a definite different identity fences us.
    """
    if execution_id is None:
        return False
    try:
        fresh = deps.store.read()
    except Exception:  # noqa: BLE001 — a transient read error must not invent ownership loss
        return False
    return fresh.execution_id != execution_id


def _dropped(
    req: IterationRequest, counters: RunCounters, reason: str
) -> IterationOutcome:
    return IterationOutcome(
        state=WorkflowState.RUNNING, action="dropped", dropped=True,
        reason=reason, run_id=req.run_id, index=req.index, counters=counters,
    )


async def _snapshot(deps: RunnerDeps) -> Mapping[str, Any]:
    snap = deps.probe.snapshot()
    if asyncio.iscoroutine(snap):
        snap = await snap
    return snap


async def _business_facts(deps: RunnerDeps) -> Mapping[str, Any]:
    facts = deps.business_facts()
    if asyncio.iscoroutine(facts):
        facts = await facts
    return dict(facts)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_epoch() -> float:
    return datetime.now(UTC).timestamp()
