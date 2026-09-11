"""Pipeline FRAMEWORK-layer validation (sealed spec, step 2 of the batched plan).

Verifies the orchestrator skeleton — NOT the 10 business handlers — along the
three core paths the spec pins:

A. normal advance: decide→validate→advance, fence + gate really live in the loop;
B. degraded advance: budget/timeout/invalid-decision faults land in the fixed
   ledger and the stage transition is STILL forced (never spin in place);
C. structural stop: first occurrence → ledger persists → run terminalizes BLOCKED
   through CAUSE_STRUCTURAL in the SAME grading pass (no re-run of the dead node).

Hard rule for every path: assertions must prove ``node_entry_fence`` and the
``llm_gate`` were actually exercised — a control-flow test that quietly bypassed
the infrastructure is exactly what this file exists to catch.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from uuid import uuid4

import pytest

import plugins.research.pipeline as pipeline
from plugins.research.llm_budget import RunBudget, StageGate
from plugins.research.pipeline import (
    CONTRACTS,
    DegradedDecision,
    NodeCtx,
    StageContract,
    StructuralStop,
    make_ledger_entry,
    parse_json_reply,
    render_ledger,
    run_node,
)
from plugins.research.plugin import ResearchService, get_auto_run_fence
from plugins.research.workflow_adapter import CostLimitExceeded, grade_turn, TurnFacts
from workflow.policy import (
    CAUSE_CANCEL,
    CAUSE_FINISHED,
    CAUSE_STRUCTURAL,
    IterationFacts,
    LoopPolicy,
    WorkflowState,
)

OWNER = uuid4()


def _iso(delta_s: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - delta_s))


@pytest.fixture
def service(tmp_path) -> ResearchService:
    return ResearchService(drive=None, scratch_root=tmp_path / "scratch")


def _create(service, task_id, stage="FRAME"):
    pdir = service._project_dir(OWNER, task_id)
    pdir.mkdir(parents=True, exist_ok=True)
    service._save_json(pdir / "project.json", {
        "id": task_id, "owner_id": str(OWNER), "name": "pipe-test",
        "profile": "research", "stage": stage, "gates": {},
        "project_revision": 0, "updated_at": _iso(),
    })
    service._save_json(pdir / "approvals.json", {"approvals": []})
    service._save_json(pdir / "executions.json", {"executions": []})
    service._save_json(pdir / "graph.json", {"nodes": [], "edges": []})


def _started(service, task_id):
    """begin_run + ledger execution_id aligned (mirrors the fenced-write tests)."""
    rid = service.begin_run(OWNER, task_id)["run_id"]
    base = service.get_driver_checkpoint(OWNER, task_id)
    service.atomic_update_project(
        OWNER, task_id,
        lambda p: p.__setitem__(
            "driver", {**base, "run_id": rid, "execution_id": f"{rid}:1:1"}
        ),
    )
    return rid


def _seam(monkeypatch, replies):
    """PIPELINE_LLM_CALL stub; records every prompt, returns replies in order."""
    seen: list[str] = []

    async def fake(prompt: str, system: str) -> str:
        seen.append(prompt)
        return replies[min(len(seen) - 1, len(replies) - 1)]

    monkeypatch.setattr(pipeline, "PIPELINE_LLM_CALL", fake)
    return seen


def _run(service, task_id, rid, handlers, **kw):
    return run_node(
        service, OWNER, task_id, run_id=rid, execution_id=f"{rid}:1:1",
        turn_index=1, handlers=handlers, **kw,
    )


# ═════════════════════════════ A. normal advance ═════════════════════════════

async def test_normal_path_advances_with_fence_and_gate(service, monkeypatch):
    task = "a" + uuid.uuid4().hex[:8]
    _create(service, task, stage="FRAME")
    rid = _started(service, task)
    seen = _seam(monkeypatch, ['{"md": "ok"}'])
    fence_inside = {}

    async def handler(ctx: NodeCtx):
        fence_inside.update(get_auto_run_fence() or {})
        payload = await ctx.decide('{"md": ...}', system="s", validate=lambda d: [])
        ctx.facts["payload"] = payload

    out = await _run(service, task, rid, {"FRAME": handler})
    # control flow
    assert out.kind == "advanced" and out.next_stage == "EVIDENCE"
    assert service.read_project(OWNER, task)["stage"] == "EVIDENCE"
    # infrastructure really exercised (not bypassed):
    assert len(seen) == 1                                   # exactly one completion
    assert fence_inside.get("run_id") == rid                # fence LIVE inside handler
    assert get_auto_run_fence() is None                     # and cleared on exit
    node = service.read_project(OWNER, task)["pipeline"]["last_node"]
    assert node["budget"]["calls"] == 1 and node["budget"]["tokens_in_est"] > 0
    # no channel/pricing in tests: the gap is explicit, never $0-laundered
    assert node["budget"]["pricing_unknown_calls"] == 1
    assert out.ledger == []


# ═══════════════════════ B. degraded faults advance honestly ═════════════════

LEDGER_KEYS = {"stage", "attempt", "error_class", "detail", "missing", "impact"}


async def test_degraded_decision_repairs_once_then_forces_advance(service, monkeypatch):
    task = "b" + uuid.uuid4().hex[:8]
    _create(service, task, stage="FRAME")
    rid = _started(service, task)
    seen = _seam(monkeypatch, ['{"wrong": 1}', '{"wrong": 2}'])

    async def handler(ctx: NodeCtx):
        await ctx.decide("PROMPT", system="s", validate=lambda d: ["field x required"])

    out = await _run(service, task, rid, {"FRAME": handler})
    # repair-once: exactly TWO completions, the 2nd = same input + violations only
    assert len(seen) == 2
    assert seen[1].startswith("PROMPT") and "VIOLATIONS" in seen[1]
    assert "field x required" in seen[1]
    # ledger + honest advance
    assert out.kind == "advanced"
    assert service.read_project(OWNER, task)["stage"] == "EVIDENCE"
    assert len(out.ledger) == 1
    assert set(out.ledger[0]) == LEDGER_KEYS
    assert out.ledger[0]["error_class"] == "degraded_decision"
    assert "degraded_decision" in out.turn_value


async def test_node_timeout_ledgers_and_forces_advance(service, monkeypatch):
    task = "c" + uuid.uuid4().hex[:8]
    _create(service, task, stage="EVIDENCE")
    rid = _started(service, task)
    monkeypatch.setitem(
        CONTRACTS, "EVIDENCE", StageContract("EVIDENCE", 6, node_budget_s=0.05)
    )

    async def slow(ctx: NodeCtx):
        await asyncio.sleep(0.5)

    out = await _run(service, task, rid, {"EVIDENCE": slow})
    assert out.kind == "advanced"  # floor timeout never parks the chain
    assert out.ledger[0]["error_class"] == "node_timeout"
    assert service.read_project(OWNER, task)["stage"] == "DESIGN"


async def test_stage_budget_spent_ledgers_and_forces_advance(service, monkeypatch):
    task = "d" + uuid.uuid4().hex[:8]
    _create(service, task, stage="FRAME")
    rid = _started(service, task)
    _seam(monkeypatch, ['{}'])

    async def greedy(ctx: NodeCtx):
        for _ in range(CONTRACTS["FRAME"].llm_calls + 1):
            await ctx.complete("q", system="s")

    out = await _run(service, task, rid, {"FRAME": greedy})
    assert out.kind == "advanced" and out.ledger[0]["error_class"] == "llm_budget_exceeded"
    # declared budget is the hard ceiling: exactly llm_calls completions happened
    node = service.read_project(OWNER, task)["pipeline"]["last_node"]
    assert node["budget"]["calls"] == CONTRACTS["FRAME"].llm_calls


async def test_cost_fuse_re_raises(service, monkeypatch):
    task = "e" + uuid.uuid4().hex[:8]
    _create(service, task, stage="FRAME")
    rid = _started(service, task)
    seen = _seam(monkeypatch, ['{}'])

    async def handler(ctx: NodeCtx):
        await ctx.complete("q", system="s")

    with pytest.raises(CostLimitExceeded):
        await _run(service, task, rid, {"FRAME": handler},
                   max_cost_usd=0.40, start_spent_usd=0.40)
    # the hard fuse fires in admit — the transport was never touched
    assert seen == []
    assert get_auto_run_fence() is None  # fence unwound on the raising path too


# ═══════════════════ C. structural stop: immediate BLOCKED, no re-run ════════

async def test_structural_stop_persists_and_skips_transition(service, monkeypatch):
    task = "g" + uuid.uuid4().hex[:8]
    _create(service, task, stage="FRAME")
    rid = _started(service, task)
    transitioned = []
    real_transition = service.transition_stage

    def spy(owner_id, project_id, **kw):
        transitioned.append(kw.get("target"))
        return real_transition(owner_id, project_id, **kw)

    monkeypatch.setattr(service, "transition_stage", spy)

    async def doomed(ctx: NodeCtx):
        raise StructuralStop("FRAME", "research_question", "no falsifiable question")

    out = await _run(service, task, rid, {"FRAME": doomed})
    assert out.kind == "blocked" and out.structural["missing"] == "research_question"
    assert transitioned == []  # the dead node never tried to advance
    assert service.read_project(OWNER, task)["stage"] == "FRAME"  # state untouched
    flag = service.read_project(OWNER, task)["pipeline"]["structural_stop"]
    assert flag["stage"] == "FRAME" and flag["missing"] == "research_question"
    # re-entry guard: even if a successor slipped through, the node short-circuits
    called = []

    async def witness(ctx: NodeCtx):
        called.append(1)

    out2 = await _run(service, task, rid, {"FRAME": witness})
    assert out2.kind == "blocked" and called == []
    assert "already structurally blocked" in out2.turn_value


async def test_auto_turn_terminalizes_structural_blocked_once(service):
    """Full-chain proof: the structural flag set DURING the turn is picked up by
    the SAME grading pass → BLOCKED terminal, no N+1 scheduling, and — unlike the
    other cap parks — NO auto-settle walk to PUBLISH."""
    from plugins.research.driver import ResearchRunDriver, RunState

    task = "h" + uuid.uuid4().hex[:8]
    _create(service, task, stage="FRAME")
    run = service.begin_run(OWNER, task)

    async def run_turn(prompt):
        # minimal pipeline node: mark structural, return like the worker seam would
        service.atomic_update_project(
            OWNER, task,
            lambda p: p.__setitem__(
                "pipeline", {"structural_stop": {"stage": "WRITE", "missing": "draft"}}
            ),
        )
        from plugins.research.driver import RunTurnResult
        return RunTurnResult(final_answer="PIPELINE WRITE: STRUCTURAL STOP", cost_usd=0.05)

    outcome = await ResearchRunDriver().auto_turn(
        service, owner_id=OWNER, task_id=task, run_id=run["run_id"],
        turn_index=1, run_turn=run_turn,
    )
    assert outcome.state == RunState.BLOCKED
    assert "structural" in (outcome.reason or "")
    assert getattr(outcome, "next_turn_index", None) is None  # never scheduled again
    project = service.read_project(OWNER, task)
    assert project["stage"] == "FRAME"          # NO auto-settle walked it to PUBLISH
    assert project["last_block"]["kind"] == "blocked"
    assert outcome.final_answer == "PIPELINE WRITE: STRUCTURAL STOP"


async def test_auto_turn_pipeline_turn_advances_and_meters_spend(service):
    """Path A at the driver seam: spend flows TaskResult → counters → ledger."""
    from plugins.research.driver import ResearchRunDriver, RunTurnResult, RunState

    task = "i" + uuid.uuid4().hex[:8]
    _create(service, task, stage="FRAME")

    driver = ResearchRunDriver()
    run = service.begin_run(OWNER, task)

    async def run_turn(prompt):
        # what a pipeline node's tail does inside the driver-minted fence:
        # a legal transition committed under the live F2 identity.
        out = service.transition_stage(OWNER, task, target="EVIDENCE",
                                       expected_current_stage="FRAME")
        assert out.get("transition") == "ADVANCED"
        return RunTurnResult(final_answer="PIPELINE FRAME -> EVIDENCE: ADVANCED",
                             cost_usd=0.07)

    outcome = await driver.auto_turn(
        service, owner_id=OWNER, task_id=task, run_id=run["run_id"],
        turn_index=1, run_turn=run_turn,
    )
    assert outcome.state == RunState.RUNNING and outcome.action == "continue"
    assert outcome.next_turn_index == 2
    led = service.get_driver_checkpoint(OWNER, task)
    assert abs(led["cumulative_cost_usd"] - 0.07) < 1e-9


# ══════════════════════ pure helpers / schema pins ═══════════════════════════

def test_ledger_schema_is_closed():
    ok = make_ledger_entry(stage="WRITE", attempt=1, error_class="node_timeout",
                           detail="d", missing="", impact="i")
    assert set(ok) == LEDGER_KEYS
    with pytest.raises(ValueError):
        make_ledger_entry(stage="WRITE", attempt=1, error_class="oops",
                          detail="d", missing="", impact="i")


def test_parse_json_reply_survives_fences_and_prose():
    assert parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_reply('Sure! {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}
    # the FIRST balanced object wins; trailing prose or junk is ignored
    assert parse_json_reply('{"a": 1} {"b": 2}') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_reply("no json here")
    with pytest.raises(ValueError):
        parse_json_reply('{"a": 1')  # unbalanced


def test_grade_turn_structural_slot_and_precedence():
    grade = grade_turn(TurnFacts(
        stage="WRITE", progress=False, consecutive_no_progress=5,
        max_no_progress=2, max_turns=1, max_cost_usd=0.0, structural_stop=True,
    ))
    assert grade.state.name == "BLOCKED"  # beats stall AND both caps
    assert "structural" in grade.reason

    # cancel still wins over everything
    g2 = grade_turn(TurnFacts(stage="WRITE", cancel_requested=True, structural_stop=True))
    assert g2.state.name == "CANCELLED"


def test_core_chain_position():
    grade = LoopPolicy().grade(IterationFacts(finished=True, structural_stop=True))
    assert grade.cause == CAUSE_FINISHED  # business layer owns finished-suppression
    g2 = LoopPolicy().grade(IterationFacts(structural_stop=True))
    assert g2.state == WorkflowState.FAILED and g2.cause == CAUSE_STRUCTURAL


def test_render_ledger_texts():
    e = make_ledger_entry(stage="REVIEW", attempt=1, error_class="handler_error",
                          detail="boom", missing="", impact="unreviewed edition")
    txt = render_ledger([e], reviewed=False)
    assert "handler_error" in txt and "boom" in txt and "UNREVIEWED" in txt


def test_contracts_declared_budgets_match_sealed_spec():
    assert CONTRACTS["PUBLISH"].llm_calls == 0
    assert CONTRACTS["WRITE"].thinking is True
    assert all(not c.thinking for s, c in CONTRACTS.items() if s != "WRITE")
    assert all(0 < c.node_budget_s <= 120 for c in CONTRACTS.values())
