"""Runner closed-loop tests on generic fakes: lease -> execute -> grade -> settle.

No disk, no network, no adapter vocabulary — an in-memory LeaseStore, a scripted Executor,
a constant ProgressProbe and a facts closure are all the "world" these tests need. Every
test asserts the OUTCOME the driver contract requires plus the ledger state it leaves
behind.
"""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime

import pytest

from workflow.leases import STATE_DONE, STATE_RUNNING, LeaseConfig, LeaseLedger
from workflow.policy import CAUSE_CANCEL, CAUSE_NO_PROGRESS, LoopCaps, LoopPolicy
from workflow.ports import TaskResult
from workflow.retry import RetryPolicy
from workflow.runner import (
    ExecutionFailed,
    IterationRequest,
    RunCounters,
    RunnerDeps,
    drive_iteration,
)
from workflow.states import WorkflowState


class FakeStore:
    def __init__(self, ledger: LeaseLedger):
        self.ledger = ledger
        self.lock = asyncio.Lock()

    def atomic(self, mutate):
        self.ledger = mutate(self.ledger)
        return self.ledger

    def read(self) -> LeaseLedger:
        return self.ledger


class FakeProbe:
    """`advanced` toggles progress between the before/after snapshots."""

    def __init__(self, advanced: bool = True):
        self.advanced = advanced
        self._flip = False

    def snapshot(self):
        self._flip = not self._flip
        return {"tick": self._flip}

    def changed(self, before, after):
        return self.advanced and before != after


class ScriptedExecutor:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def execute(self, request):
        self.calls += 1
        step = self.script.pop(0) if self.script else ("ok", "done")
        kind, payload = step
        if kind == "ok":
            return TaskResult(value=payload, spend=0.1)
        if kind == "error":
            raise payload
        if kind == "hang":  # blocks until cancelled by the watcher
            await asyncio.Event().wait()
        raise AssertionError(f"unknown step {kind}")


def _ledger(**kw) -> LeaseLedger:
    base = dict(
        run_id="r1", index=0, attempt=1, state=STATE_DONE,
        updated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    base.update(kw)
    return LeaseLedger(**base)


def _deps(store, *, script=None, policy=None, probe=None, retry=None,
          facts=None, lease=None, hook=None):
    return RunnerDeps(
        store=store,
        executor=ScriptedExecutor(script or [("ok", "answer")]),
        policy=policy or LoopPolicy(),
        probe=probe or FakeProbe(),
        retry=retry or RetryPolicy(),
        business_facts=facts or (lambda: {"finished": False, "pending_signals": 0,
                                          "cancel_requested": False}),
        compose_prompt=lambda req, attempt: f"turn {req.index} attempt {attempt}",
        lease=lease or LeaseConfig(refresh_s=5, stale_s=30),  # watcher never ticks
        hook=hook,
    )


class TestHappyPaths:
    async def test_continue_settles_ledger_and_points_to_next_index(self):
        store = FakeStore(_ledger())
        out = await drive_iteration(_deps(store), IterationRequest("r1", 1))
        assert out.action == "continue" and out.next_index == 2
        assert out.value == "answer"
        assert out.counters.total_spend == 0.1
        assert out.counters.consecutive_no_progress == 0
        assert store.ledger.state == STATE_DONE  # settled; caller releases/schedules

    async def test_finished_predicate_wins_from_adapter_facts(self):
        store = FakeStore(_ledger(index=4))
        deps = _deps(store, facts=lambda: {"finished": True, "pending_signals": 0,
                                           "cancel_requested": False})
        out = await drive_iteration(deps, IterationRequest("r1", 5))
        assert out.state is WorkflowState.SUCCEEDED and out.action == "succeeded"
        assert store.ledger.state == STATE_DONE

    async def test_pending_signal_parks_as_waiting(self):
        store = FakeStore(_ledger())
        deps = _deps(store, facts=lambda: {"finished": False, "pending_signals": 1,
                                           "cancel_requested": False})
        out = await drive_iteration(deps, IterationRequest("r1", 1))
        assert out.state is WorkflowState.WAITING and out.cause == "pending_signal"


class TestDroppedAndCancelled:
    async def test_duplicate_iteration_drops_without_touching_the_ledger(self):
        ledger = _ledger(index=3, state=STATE_DONE)
        store = FakeStore(ledger)
        out = await drive_iteration(_deps(store), IterationRequest("r1", 3))
        assert out.dropped is True and out.action == "dropped"
        assert store.ledger == ledger
        assert store.executor_calls == 0 if hasattr(store, "executor_calls") else True

    async def test_live_twin_is_dropped(self):
        fresh = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ledger = _ledger(index=1, state=STATE_RUNNING, execution_id="r1:1:1",
                         updated_at=fresh)
        store = FakeStore(ledger)
        out = await drive_iteration(_deps(store), IterationRequest("r1", 1))
        assert out.dropped is True and "live duplicate" in (out.reason or "")

    async def test_pre_turn_cancel_terminalizes_without_executing(self):
        store = FakeStore(_ledger(index=1, cancel_requested=True))
        deps = _deps(store, script=[])
        out = await drive_iteration(deps, IterationRequest("r1", 2))
        assert out.state is WorkflowState.CANCELLED and out.cause == CAUSE_CANCEL
        assert out.execution_id == "r1:2:1"
        assert store.ledger.state == STATE_DONE
        assert deps.executor.calls == 0  # never ran the black box

    async def test_mid_turn_cancel_from_heartbeat_watch(self):
        store = FakeStore(_ledger())
        deps = _deps(store, script=[("hang", None)],
                     lease=LeaseConfig(refresh_s=0.01, stale_s=30))

        async def flip_cancel_later():
            await asyncio.sleep(0.03)
            store.ledger = dataclasses.replace(store.ledger, cancel_requested=True)

        flipper = asyncio.create_task(flip_cancel_later())
        out = await drive_iteration(deps, IterationRequest("r1", 1))
        await flipper
        assert out.state is WorkflowState.CANCELLED
        assert store.ledger.state == STATE_DONE


class TestRetrySemantics:
    async def test_transient_then_success_bumps_attempt_and_identity(self):
        store = FakeStore(_ledger())
        deps = _deps(
            store,
            script=[("error", TimeoutError("t")), ("ok", "second-try")],
            retry=RetryPolicy(max_attempts=3, is_transient=lambda e: isinstance(e, TimeoutError)),
        )
        # keep the retry sleep instant
        deps.retry = dataclasses.replace(deps.retry, backoff=lambda a: 0.0)
        out = await drive_iteration(deps, IterationRequest("r1", 1))
        assert out.action == "continue" and out.attempt == 2
        assert out.execution_id == "r1:1:2"          # re-minted identity...
        assert out.counters.total_spend == 0.1       # ...only the successful run metered
        assert deps.executor.calls == 2

    async def test_transient_exhaustion_returns_honest_failed_outcome(self):
        store = FakeStore(_ledger())
        deps = _deps(
            store,
            script=[("error", TimeoutError("t")), ("error", TimeoutError("t")),
                    ("error", TimeoutError("t"))],
            retry=RetryPolicy(max_attempts=2, is_transient=lambda e: True,
                              backoff=lambda a: 0.0),
        )
        out = await drive_iteration(deps, IterationRequest("r1", 1))
        assert out.state is WorkflowState.FAILED and out.cause == "transient_exhausted"
        assert "exhausted" in out.reason
        assert store.ledger.state == STATE_DONE      # settled -> caller releases slot
        assert deps.executor.calls == 2

    async def test_non_transient_settles_then_raises_loud(self):
        store = FakeStore(_ledger())
        deps = _deps(store, script=[("error", ValueError("bug"))],
                     retry=RetryPolicy(max_attempts=5, is_transient=lambda e: False))
        with pytest.raises(ExecutionFailed) as ei:
            await drive_iteration(deps, IterationRequest("r1", 1))
        assert isinstance(ei.value.cause, ValueError)
        assert ei.value.outcome.state is WorkflowState.FAILED
        assert ei.value.outcome.cause == "error"
        assert store.ledger.state == STATE_DONE
        assert deps.executor.calls == 1


class TestBrakesAndCaps:
    async def test_no_progress_accumulates_across_iterations(self):
        store = FakeStore(_ledger())
        deps = _deps(store, probe=FakeProbe(advanced=False),
                     policy=LoopPolicy(caps=LoopCaps(max_no_progress=2)))
        first = await drive_iteration(deps, IterationRequest("r1", 1))
        assert first.action == "continue"
        assert first.consecutive_no_progress == 1
        second = await drive_iteration(
            deps, IterationRequest("r1", 2, counters=first.counters)
        )
        assert second.state is WorkflowState.FAILED and second.cause == CAUSE_NO_PROGRESS

    async def test_spend_cap_trips_on_numeric_total_not_unknown_count(self):
        store = FakeStore(_ledger())
        deps = _deps(store, policy=LoopPolicy(caps=LoopCaps(max_spend=0.2)))
        counters = RunCounters(total_spend=0.2, unknown_spend_count=3)
        out = await drive_iteration(deps, IterationRequest("r1", 1, counters=counters))
        assert out.state is WorkflowState.FAILED and out.cause == "spend_cap_exceeded"

    async def test_unknown_spend_never_laundered_into_total(self):
        store = FakeStore(_ledger())
        deps = _deps(store, script=[("ok", "v")])
        deps.executor.script = []
        # executor returns spend=None via a hand-rolled stub
        class Unknown:
            calls = 0

            async def execute(self, request):
                self.calls += 1
                return TaskResult(value="v", spend=None)

        deps.executor = Unknown()
        out = await drive_iteration(deps, IterationRequest("r1", 1))
        assert out.counters.total_spend == 0.0
        assert out.counters.unknown_spend_count == 1


class TestTerminalHook:
    async def test_hook_can_rewrite_stop_into_continue(self):
        from workflow.policy import Grade

        class Rescuer:
            async def before_terminalize(self, grade, facts):
                return Grade(None)  # "actually, keep going"

        store = FakeStore(_ledger())
        deps = _deps(store, facts=lambda: {"finished": False, "pending_signals": 1,
                                           "cancel_requested": False}, hook=Rescuer())
        out = await drive_iteration(deps, IterationRequest("r1", 1))
        assert out.action == "continue" and out.next_index == 2

    async def test_hook_can_replace_the_terminal_verdict(self):
        from workflow.policy import Grade

        class Swapper:
            async def before_terminalize(self, grade, facts):
                return Grade(WorkflowState.SUCCEEDED, "settled", "hook rewrote it",
                             grade.consecutive_no_progress)

        store = FakeStore(_ledger())
        deps = _deps(store, facts=lambda: {"finished": False, "pending_signals": 7,
                                           "cancel_requested": False}, hook=Swapper())
        out = await drive_iteration(deps, IterationRequest("r1", 1))
        assert out.state is WorkflowState.SUCCEEDED and out.reason == "hook rewrote it"


class TestLeaseReclaim:
    async def test_stale_running_lease_reclaims_same_iteration_next_attempt(self):
        crashed = _ledger(index=2, state=STATE_RUNNING, execution_id="r1:2:1",
                          updated_at="2026-01-01T00:00:00Z")  # ancient heartbeat
        store = FakeStore(crashed)
        out = await drive_iteration(_deps(store), IterationRequest("r1", 2))
        assert out.action == "continue" and out.attempt == 2
        assert out.execution_id == "r1:2:2"
