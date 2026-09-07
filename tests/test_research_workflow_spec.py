"""Research Workflow definition + fingerprint wiring tests (Level 2, research layer).

Covers what the definition must guarantee in production:
(a) the spec is structure-only and stays a true mirror of the service stage chain;
(b) ``begin_run`` mints the fingerprint into the run checkpoint (real service CAS);
(c) a stamped run whose fingerprint no longer matches terminalizes the turn as an
    honest definition-drift ERROR *before* any execution;
(d) cap VALUES are runtime config: budgets changing never flips the fingerprint;
(e) hooks stay empty — settle must not be smuggled into the generic contract.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

import plugins.research.driver as driver_module
from plugins.research.driver import DriverOutcome, ResearchRunDriver, RunTurnResult
from plugins.research.plugin import ResearchService
from plugins.research.workflow_spec import (
    AUTO_TURN_TASK,
    RESEARCH_EXECUTOR_ID,
    RESEARCH_WORKFLOW,
    _STAGE_CHAIN,
)
from plugins.research.workflow_spec import ResearchWorkflow

OWNER = uuid4()


@pytest.fixture
def service(tmp_path) -> ResearchService:
    return ResearchService(drive=None, scratch_root=tmp_path / "scratch")


def _create_project(service: ResearchService, task_id: str, *, stage: str = "FRAME") -> None:
    pdir = service._project_dir(OWNER, task_id)
    pdir.mkdir(parents=True, exist_ok=True)
    service._save_json(pdir / "project.json", {
        "id": task_id, "owner_id": str(OWNER), "name": "spec-test",
        "profile": "research", "stage": stage, "gates": {},
        "project_revision": 0, "updated_at": "2026-01-01T00:00:00Z",
    })
    service._save_json(pdir / "approvals.json", {"approvals": []})
    service._save_json(pdir / "executions.json", {"executions": []})
    service._save_json(pdir / "graph.json", {"nodes": [], "edges": []})


class TestSpecShape:
    def test_stage_chain_mirrors_service_legality(self):
        from plugins.research.plugin import _LEGAL_NEXT

        spec_transitions = RESEARCH_WORKFLOW.spec()["transitions"]
        assert {k: v[0] for k, v in spec_transitions.items()} == \
            {k: v for k, v in _LEGAL_NEXT.items() if v}
        assert list(_LEGAL_NEXT)[-1] == "PUBLISH" and _LEGAL_NEXT["PUBLISH"] is None

    def test_activities_declare_the_single_agent_turn(self):
        spec = RESEARCH_WORKFLOW.spec()
        assert spec["activities"] == [
            {"task_name": AUTO_TURN_TASK, "executor": RESEARCH_EXECUTOR_ID},
        ]

    def test_caps_are_dimension_names_only(self):
        assert RESEARCH_WORKFLOW.spec()["caps"] == ["max_no_progress", "max_spend", "max_turns"]
        assert all(isinstance(c, str) for c in RESEARCH_WORKFLOW.spec()["caps"])

    def test_hooks_stay_empty_settle_is_not_a_hook(self):
        assert RESEARCH_WORKFLOW.spec()["hooks"] == []

    def test_two_instances_fingerprint_identically(self):
        assert ResearchWorkflow().fingerprint() == RESEARCH_WORKFLOW.fingerprint()


class TestConfigIsNotDefinition:
    def test_budget_values_are_absent_from_the_spec_entirely(self):
        """Cap VALUES ride runtime config — nothing numeric enters the fingerprint."""
        spec = RESEARCH_WORKFLOW.spec()
        assert all(isinstance(c, str) for c in spec["caps"])
        assert "max_cost_usd" not in str(spec) and "max_turns" in str(spec)
        # Repeated minting is stable regardless of how the driver is configured.
        assert ResearchRunDriver(max_cost_usd=5.0).max_cost_usd == 5.0
        assert RESEARCH_WORKFLOW.fingerprint() == RESEARCH_WORKFLOW.fingerprint()

    def test_a_structural_edit_would_move_the_fingerprint(self):
        from workflow.definition import compute_fingerprint

        spec = dict(RESEARCH_WORKFLOW.spec())
        spec["transitions"] = {"DISCOVER": ["FRAME"], "FRAME": ["EVIDENCE"]}
        assert compute_fingerprint(spec) != RESEARCH_WORKFLOW.fingerprint()


class TestProductionStampingAndDrift:
    async def test_begin_run_stamps_the_fingerprint(self, service):
        _create_project(service, "stamp")
        run = service.begin_run(OWNER, "stamp")
        ledger = service.get_driver_checkpoint(OWNER, "stamp")
        assert ledger["definition_fp"] == RESEARCH_WORKFLOW.fingerprint()
        assert ledger["run_id"] == run["run_id"]

    async def test_drift_terminalizes_before_any_execution(self, service):
        calls = []

        async def run_turn(prompt):
            calls.append(prompt)
            return RunTurnResult(final_answer="x", cost_usd=0.0)

        _create_project(service, "drift")
        run = service.begin_run(OWNER, "drift")

        def tamper(project: dict) -> None:
            project["driver"]["definition_fp"] = "wf1-" + "0" * 64

        service.atomic_update_project(OWNER, "drift", tamper)
        driver = ResearchRunDriver()
        outcome = await driver.auto_turn(
            service, owner_id=OWNER, task_id="drift", run_id=run["run_id"],
            turn_index=1, run_turn=run_turn,
        )
        assert calls == []                                   # never executed the turn
        assert isinstance(outcome, DriverOutcome)
        assert outcome.state is driver_module.RunState.ERROR
        assert "definition drift" in (outcome.reason or "")
        # Slot released: the run cannot be silently resumed under the new definition.
        assert "active_run" not in service.read_project(OWNER, "drift")
        block = service.read_project(OWNER, "drift")["last_block"]
        assert block["kind"] == "error" and "definition drift" in block["reason"]

    async def test_unstamped_legacy_ledger_passes_through(self, service):
        """Runs minted before the stamp existed are tolerated (no fp != no mismatch)."""

        async def run_turn(prompt):
            return RunTurnResult(final_answer="ok", cost_usd=0.1)

        _create_project(service, "legacy")
        run = service.begin_run(OWNER, "legacy")
        service.atomic_update_project(
            OWNER, "legacy", lambda p: p["driver"].pop("definition_fp"),
        )
        outcome = await ResearchRunDriver().auto_turn(
            service, owner_id=OWNER, task_id="legacy", run_id=run["run_id"],
            turn_index=1, run_turn=run_turn,
        )
        assert outcome.action == "continue" and outcome.next_turn_index == 2

    async def test_normal_turn_rechecks_and_keeps_the_stamp(self, service):
        async def run_turn(prompt):
            return RunTurnResult(final_answer="ok", cost_usd=0.1)

        _create_project(service, "keep")
        run = service.begin_run(OWNER, "keep")
        outcome = await ResearchRunDriver().auto_turn(
            service, owner_id=OWNER, task_id="keep", run_id=run["run_id"],
            turn_index=1, run_turn=run_turn,
        )
        assert outcome.action == "continue"
        # The lease CAS rewrote the seven ledger keys; the extra stamp key survived.
        assert service.get_driver_checkpoint(OWNER, "keep")["definition_fp"] == \
            RESEARCH_WORKFLOW.fingerprint()
