"""Generic-workflow contract tests for the runtime seam (Level 2 evidence).

Nothing here may import plugins, apps, agent, skill or LLM code: the whole point of
:mod:`workflow.runtime` is that a *second* workflow — the document pipeline below —
runs end-to-end on the same core with only a definition, a registry binding table and
plain ports. Three things are proven:

(a) resolution: ``task_name -> executor id -> registry.resolve`` is a real call chain
    (per-iteration re-resolution must follow the flow's current state);
(b) strictness: caps declared by the definition need values, extra values are refused,
    undeclared activities are refused — the spec is a contract, not a suggestion;
(c) core defaults survive: without injected outcomes a cap trips FAILED (the generic
    brake), while an adapter may declare WAITING (the research park) — the SAME
    declaration mechanism, two different verdicts.
"""
from __future__ import annotations

from datetime import UTC, datetime

from workflow.definition import validate_spec
from workflow.leases import STATE_DONE, LeaseConfig, LeaseLedger
from workflow.policy import LoopPolicy
from workflow.ports import TaskResult
from workflow.retry import RetryPolicy
from workflow.runner import RunCounters, drive_iteration
from workflow.runtime import MappingRegistry, build_deps, build_loop_policy, resolve_executor
from workflow.states import WorkflowState


class FakeStore:
    def __init__(self):
        self.ledger = LeaseLedger(
            run_id=None, index=0, attempt=1, state=STATE_DONE, execution_id=None,
            updated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            cancel_requested=False,
        )

    def atomic(self, mutate):
        self.ledger = mutate(self.ledger)
        return self.ledger

    def read(self):
        return self.ledger


class CountingExecutor:
    """A named black box: records prompts, always succeeds, meters a known spend."""

    def __init__(self, name):
        self.name = name
        self.prompts: list[str] = []

    async def execute(self, request):
        self.prompts.append(request.prompt)
        return TaskResult(value=self.name, spend=0.01)


class StaticProbe:
    def snapshot(self):
        return {}

    def changed(self, before, after):
        return True


class Definition:
    """Minimal WorkflowDefinition duck: ``name`` + ``spec()`` (validated shape)."""

    def __init__(self, spec):
        self._spec = validate_spec(spec)
        self.name = self._spec["name"]

    def spec(self):
        return dict(self._spec)


def _pipeline(terminal="PUBLISH"):
    return Definition({
        "name": "document-pipeline",
        "transitions": {"EXTRACT": ["TRANSFORM"], "TRANSFORM": ["PUBLISH"]},
        "activities": [
            {"task_name": "extract", "executor": "document-extractor"},
            {"task_name": "transform", "executor": "document-transformer"},
            {"task_name": "publish", "executor": "document-publisher"},
        ],
        "caps": ["max_turns", "max_spend"],
        "hooks": [],
    })


_NO_ACTIVITY = Definition({
    "name": "single-shot",
    "transitions": {"A": ["B"]},
    "activities": [{"task_name": "do", "executor": "worker-one"}],
    "caps": ["max_turns"],
    "hooks": [],
})


def _registry():
    return MappingRegistry({
        "document-extractor": CountingExecutor("extractor"),
        "document-transformer": CountingExecutor("transformer"),
        "document-publisher": CountingExecutor("publisher"),
    })


class TestResolution:
    def test_single_activity_needs_no_task_name(self):
        reg = MappingRegistry({"worker-one": CountingExecutor("only")})
        ex = resolve_executor(_NO_ACTIVITY, reg)
        assert ex.name == "only"

    def test_multi_activity_selects_by_task_name(self):
        reg = _registry()
        assert resolve_executor(_pipeline(), reg, task_name="transform").name == "transformer"

    def test_task_not_in_definition_is_refused(self):
        import pytest
        with pytest.raises(ValueError, match="not declared"):
            resolve_executor(_pipeline(), _registry(), task_name="fork")

    def test_unregistered_executor_id_is_refused(self):
        import pytest
        with pytest.raises(LookupError, match="not registered"):
            resolve_executor(_pipeline(), MappingRegistry({}), task_name="extract")

    def test_multi_activity_without_task_name_is_refused(self):
        import pytest
        with pytest.raises(ValueError, match="explicit task_name"):
            resolve_executor(_pipeline(), _registry())


class TestCapPolicy:
    def test_declared_dimensions_join_runtime_values(self):
        policy = build_loop_policy(
            _pipeline(), {"max_turns": 7, "max_spend": 1.5},
        )
        assert policy.caps.max_turns == 7 and policy.caps.max_spend == 1.5
        assert policy.caps.max_no_progress is None  # not declared by THIS flow
        # Core default: a cap is a failure to continue, unless the caller says otherwise.
        assert policy.cap_outcome is WorkflowState.FAILED

    def test_missing_value_for_declared_cap_is_refused(self):
        import pytest
        with pytest.raises(ValueError, match="missing declared"):
            build_loop_policy(_pipeline(), {"max_turns": 7})

    def test_undeclared_value_is_refused(self):
        import pytest
        with pytest.raises(ValueError, match="not declared"):
            build_loop_policy(_pipeline(), {"max_turns": 7, "max_spend": 1.0, "max_no_progress": 3})

    def test_values_outside_the_definition_never_fingerprint(self):
        # Same structure, different cap VALUES => identical fingerprint would be absurd to
        # test here (values are not in the spec at all); assert the values' absence instead.
        spec = _pipeline().spec()
        assert spec["caps"] == ["max_spend", "max_turns"]
        assert all(isinstance(c, str) for c in spec["caps"])


class TestPipelineEndToEnd:
    async def test_three_iterations_walk_extract_transform_publish(self):
        """The Level-2 chain for a NON-research workflow, on the unmodified core."""
        state = {"stage": "EXTRACT", "finished": False}

        def business_facts():
            return {
                "finished": state["finished"],
                "pending_signals": 0,
                "cancel_requested": False,
            }

        def next_stage(stage):
            return {"EXTRACT": "TRANSFORM", "TRANSFORM": "PUBLISH"}[stage]

        definition = _pipeline()
        reg = _registry()
        store = FakeStore()
        counters = RunCounters()
        index = 1
        outcomes = []
        while True:
            deps = build_deps(
                definition=definition,
                registry=reg,
                task_name=state["stage"].lower(),   # the flow's own state machine decides
                store=store,
                probe=StaticProbe(),
                business_facts=business_facts,
                compose_prompt=lambda req, attempt: f"{state['stage']} attempt {attempt}",
                retry=RetryPolicy(max_attempts=1, is_transient=lambda e: False),
                caps_values={"max_turns": 10, "max_spend": 5.0},
                lease=LeaseConfig(refresh_s=5, stale_s=30),
            )
            out = await drive_iteration(deps, _req("doc-run", index, counters))
            outcomes.append(out)
            assert out.value == {
                "EXTRACT": "extractor", "TRANSFORM": "transformer", "PUBLISH": "publisher",
            }[state["stage"]]                       # the executor chosen per current stage
            if out.action != "continue":
                break
            state["stage"] = next_stage(state["stage"])
            if state["stage"] == "PUBLISH":
                state["finished"] = True
            counters = out.counters
            index = out.next_index

        assert outcomes[-1].state is WorkflowState.SUCCEEDED
        assert outcomes[-1].cause == "finished"
        assert store.ledger.state == STATE_DONE
        # Resolution happened every iteration — prompts prove each stage's box ran.
        assert reg.resolve("document-extractor").prompts == ["EXTRACT attempt 1"]
        assert reg.resolve("document-publisher").prompts == ["PUBLISH attempt 1"]

    async def test_turn_cap_trips_failed_with_core_default_verdict(self):
        state = {"finished": False}

        def business_facts():
            return {"finished": state["finished"], "pending_signals": 0, "cancel_requested": False}

        deps = build_deps(
            definition=_NO_ACTIVITY,
            registry=MappingRegistry({"worker-one": CountingExecutor("only")}),
            store=FakeStore(),
            probe=StaticProbe(),
            business_facts=business_facts,
            compose_prompt=lambda req, attempt: "go",
            retry=RetryPolicy(max_attempts=1, is_transient=lambda e: False),
            caps_values={"max_turns": 1},
            lease=LeaseConfig(refresh_s=5, stale_s=30),
        )
        out = await drive_iteration(deps, _req("cap-run", 1, RunCounters()))
        # index=1 hits max_turns=1 — a generic workflow with no injected opinion fails...
        assert out.state is WorkflowState.FAILED and out.cause == "turn_cap_exceeded"

        # ...while the SAME declaration can be parked by an adapter (research semantics).
        policy2 = build_loop_policy(
            _NO_ACTIVITY, {"max_turns": 1},
            cap_outcome=WorkflowState.WAITING, signal_outcome=WorkflowState.WAITING,
        )
        deps2 = build_deps(
            definition=_NO_ACTIVITY,
            registry=MappingRegistry({"worker-one": CountingExecutor("only")}),
            store=FakeStore(),
            probe=StaticProbe(),
            business_facts=business_facts,
            compose_prompt=lambda req, attempt: "go",
            retry=RetryPolicy(max_attempts=1, is_transient=lambda e: False),
            policy=policy2,
            lease=LeaseConfig(refresh_s=5, stale_s=30),
        )
        out2 = await drive_iteration(deps2, _req("cap-run", 1, RunCounters()))
        assert out2.state is WorkflowState.WAITING and out2.cause == "turn_cap_exceeded"


def _req(run_id, index, counters):
    from workflow.runner import IterationRequest
    return IterationRequest(run_id, index, counters=counters)
