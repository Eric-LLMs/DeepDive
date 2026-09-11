"""Batch-2 handler validation: DESIGN / EXECUTE / EXPLAIN / WRITE.

Hard metrics pinned here:

* DESIGN=1, EXPLAIN=1, WRITE=1 on the normal path (repair-once ceiling ≤ 2);
* EXECUTE is the TWO-BEAT node: exactly LLM#1 plan + LLM#2 summary — the two
  beats fill the declared budget (2), a third completion is impossible because
  the stage gate power-cuts any hidden extra call into the ledger; the middle
  (record_execution → Python steps → finish_execution) runs at 0 LLM;
* WRITE doubles as the hard gate: both draft attempts invalid → StructuralStop
  (terminal BLOCKED, no force advance), missing question → 0-LLM structural;
* every artifact write / version stamp / graph update is pure Python.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

import plugins.research.pipeline as pipeline
import plugins.research.plugin as rplugin  # noqa: F401 — shared seams module
from core.infrastructure.request_context import set_request_user
from plugins.research.pipeline import CONTRACTS as PIPE_CONTRACTS

import plugins.research.handlers  # noqa: F401,E402 — side effect: registers batch 1+2

from agent import Context, PluginManager, SkillRegistry, ToolRuntime
from plugins.research.plugin import ResearchService, register_research_plugins

USER = uuid.uuid4()


@pytest.fixture(autouse=True)
def _request_user():
    set_request_user(USER)
    yield
    set_request_user(None)


@pytest.fixture(autouse=True)
def _no_fence():
    from plugins.research.plugin import get_auto_run_fence
    assert get_auto_run_fence() is None
    yield


@pytest.fixture
def env(tmp_path):
    from tests._drive_fakes import make_drive

    drive = make_drive(tmp_path)
    ctx = Context()
    ctx.provide("drive", drive)
    ctx.provide("research_scratch", tmp_path / "scratch")
    runtime = ToolRuntime()
    manager = PluginManager(runtime, SkillRegistry(), ctx)
    register_research_plugins(manager, ctx)
    return SimpleNamespace(ctx=ctx, drive=drive, scratch=tmp_path / "scratch")


def _seam(monkeypatch, replies):
    seen: list[str] = []

    async def fake(prompt: str, system: str) -> str:
        seen.append(prompt)
        return replies[min(len(seen) - 1, len(replies) - 1)]

    monkeypatch.setattr(pipeline, "PIPELINE_LLM_CALL", fake)
    return seen


async def _task(env, stage: str, *, question: str | None = None,
                claims=(), pipeline_seed: dict | None = None):
    svc = ResearchService(drive=env.drive, scratch_root=env.scratch)
    # The pipeline deployment runs auto-runs PROGRESSIVE: the guard gates'
    # deterministic checks still run and their failures land in diagnostics —
    # only the blocking consequence moves to the business layer, where the
    # pipeline expresses structural inability via StructuralStop. G1 (a report
    # must exist on disk to enter REVIEW) still binds, satisfied by WRITE's
    # deterministic report.md landing below.
    task = (await svc.create_task(
        USER, title="tomato research", execution_mode="progressive",
    ))["task_id"]
    rid = svc.begin_run(USER, task)["run_id"]
    base = svc.get_driver_checkpoint(USER, task)

    def _seed(p):
        p["driver"] = {**base, "run_id": rid, "execution_id": f"{rid}:1:1"}
        p["stage"] = stage
        if question is not None:
            p["research_question"] = question
        if pipeline_seed:
            p.setdefault("pipeline", {}).update(pipeline_seed)
    svc.atomic_update_project(USER, task, _seed)
    for cid, stmt in claims:
        svc.record_node(USER, task, node={
            "id": cid, "type": "Claim", "label": stmt, "statement": stmt,
            "strength": "medium",
        })
    return svc, task, rid


def _run(svc, task, rid):
    return pipeline.run_node(
        svc, USER, task, run_id=rid, execution_id=f"{rid}:1:1", turn_index=1,
    )


def _budget(svc, task):
    return svc.read_project(USER, task)["pipeline"]["last_node"]["budget"]


def _execs(svc, task):
    return svc._load_json(svc._project_dir(USER, task) / "executions.json",
                          {"executions": []})["executions"]


# ═════════════════════════════ DESIGN ════════════════════════════════════════

async def test_design_normal_path_exactly_one_llm(env, monkeypatch):
    svc, task, rid = await _task(
        env, "DESIGN",
        question="Does home cooking increase lycopene bioavailability in tomatoes?",
        claims=[("k1", "cooking raises lycopene")],
    )
    reply = json.dumps({
        "method": "Adjudicate each claim against the corpus, aggregate verdict "
                  "tickets per claim and report coverage.",
        "steps": ["verdict statistics", "evidence table", "coverage report"],
        "data_needed": ["corpus", "claim graph"],
        "success_criteria": "every claim carries a verdict or an honest gap",
    })
    seen = _seam(monkeypatch, [reply])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "EXECUTE"
    assert len(seen) == 1 and _budget(svc, task)["calls"] == 1      # HARD METRIC
    assert out.ledger == []
    art = svc.read_artifact(USER, task, artifact_id="design.md")
    assert "## Success criteria" in art["content"]
    assert svc.read_project(USER, task)["pipeline"]["design"]["steps"]


async def test_design_repair_once_then_mechanical_fallback(env, monkeypatch):
    svc, task, rid = await _task(env, "DESIGN",
                                 question="Does home cooking increase lycopene?",
                                 claims=[("k1", "x")])
    seen = _seam(monkeypatch, ['{"method": "short"}', '{"method": "short"}'])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced"                    # degradable: ledger + advance
    assert len(seen) == 2 and _budget(svc, task)["calls"] == 2      # NEVER a third
    assert [e["error_class"] for e in out.ledger] == ["degraded_decision"]
    art = svc.read_artifact(USER, task, artifact_id="design.md")
    assert "Adjudicate every recorded claim" in art["content"]      # mechanical plan


async def test_design_without_question_is_structural_zero_llm(env, monkeypatch):
    svc, task, rid = await _task(env, "DESIGN")      # no research_question seeded
    seen = _seam(monkeypatch, ["{}"])
    out = await _run(svc, task, rid)
    assert out.kind == "blocked" and out.structural["missing"] == "research_question"
    assert seen == [] and _budget(svc, task)["calls"] == 0          # 0 LLM gate


# ═════════════════════════════ EXECUTE (two-beat ceiling) ════════════════════

async def test_execute_two_beats_fill_budget_with_python_audit_chain(env, monkeypatch):
    svc, task, rid = await _task(
        env, "EXECUTE",
        question="Does home cooking increase lycopene?",
        claims=[("k1", "cooking raises lycopene"), ("k2", "raw absorbs poorly")],
        pipeline_seed={"design": {"method": "m", "steps": ["claim_stats"]}},
    )
    plan = json.dumps({"steps": [
        {"op": "claim_stats"}, {"op": "coverage_report"}, {"op": "rm_rf_root"},
    ]})
    summary = json.dumps({"summary": "2 claims, none ticketed; 0 anchored."})
    seen = _seam(monkeypatch, [plan, summary])
    out = await _run(svc, task, rid)

    assert out.kind == "advanced" and out.next_stage == "EXPLAIN"
    # HARD METRIC: exactly two beats = the FULL declared budget, ever
    assert len(seen) == 2
    snap = _budget(svc, task)
    assert snap["calls"] == PIPE_CONTRACTS["EXECUTE"].llm_calls == 2
    # beat 1 prompt is the ACTION PLAN, beat 2 is over the execution outputs
    assert "Valid ops" in seen[0] and "Execution outputs" in seen[1]
    # middle ran at 0 LLM: every whitelisted step produced a finished audit row
    rows = {r["tool"]: r for r in _execs(svc, task)}
    assert rows["pipeline.execute.claim_stats"]["status"] == "SUCCESS"
    assert rows["pipeline.execute.coverage_report"]["status"] == "SUCCESS"
    assert "rm_rf_root" not in json.dumps(rows)       # whitelist op never executed
    ex = svc.read_project(USER, task)["pipeline"]["execution"]
    assert ex["outputs"]["claim_stats"]["claims"] == 2
    assert ex["skipped_steps"] == ["rm_rf_root"]
    # the dropped non-whitelisted op is an honest ledger line, not a silent pass
    assert any(e["error_class"] == "degraded_decision" and "rm_rf_root" in e["detail"]
               for e in out.ledger)
    art = svc.read_artifact(USER, task, artifact_id="execution_report.md")
    assert "2 claims, none ticketed" in art["content"]


async def test_execute_invalid_plan_falls_back_to_full_op_sweep(env, monkeypatch):
    svc, task, rid = await _task(env, "EXECUTE", question="q?",
                                 claims=[("k1", "one")])
    garbage = "I will run a python script and also query the LLM."   # no JSON
    seen = _seam(monkeypatch, [garbage, json.dumps({"summary": "ok"})])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced"
    assert len(seen) == 2 and _budget(svc, task)["calls"] == 2       # no repair pass
    assert any("invalid action plan" in e["detail"] for e in out.ledger)
    # default sweep: all four deterministic ops audited despite the dead plan
    tools = {r["tool"] for r in _execs(svc, task)}
    assert tools == {"pipeline.execute.claim_stats", "pipeline.execute.evidence_table",
                     "pipeline.execute.coverage_report", "pipeline.execute.gap_list"}


async def test_execute_third_completion_is_structurally_impossible(env, monkeypatch):
    svc, task, rid = await _task(env, "EXECUTE", question="q?", claims=[("k1", "x")])

    async def chatty(prompt: str, system: str) -> str:
        # the handler only ever completes twice; if it tried a third the gate
        # would raise BEFORE this seam — proven by calling the gate directly.
        return json.dumps({"steps": [{"op": "claim_stats"}]}) if "Valid ops" in prompt \
            else json.dumps({"summary": "s"})

    monkeypatch.setattr(pipeline, "PIPELINE_LLM_CALL", chatty)
    out = await _run(svc, task, rid)
    assert _budget(svc, task)["calls"] == 2
    # direct proof: the stage's gate cannot admit a 3rd call anymore
    from plugins.research.llm_budget import RunBudget, StageBudgetExceeded, StageGate
    run = RunBudget(cap_usd=None)
    gate = StageGate(run, stage="EXECUTE", max_calls=PIPE_CONTRACTS["EXECUTE"].llm_calls)
    gate.admit(); gate.admit()
    with pytest.raises(StageBudgetExceeded):
        gate.admit()


# ═════════════════════════════ EXPLAIN ═══════════════════════════════════════

async def test_explain_single_call_and_honest_known_gaps(env, monkeypatch):
    svc, task, rid = await _task(
        env, "EXPLAIN", question="Why does cooking raise lycopene?",
        claims=[("k1", "cooking raises lycopene")],
    )
    reply = json.dumps({
        "explanations": [
            {"claim_id": "k1",
             "causal_line": "heat disrupts chromoplast matrices, freeing bound "
                            "lycopene for micellar uptake.", "confidence": "medium"},
            {"claim_id": "ghost9", "causal_line": "invented row", "confidence": "low"},
        ],
        "open_questions": ["Does fat content of the meal change the effect?"],
    })
    seen = _seam(monkeypatch, [reply])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "WRITE"
    assert len(seen) == 1 and _budget(svc, task)["calls"] == 1       # HARD METRIC
    ex = svc.read_project(USER, task)["pipeline"]["explain"]
    assert [r["claim_id"] for r in ex["explanations"]] == ["k1"]     # ghost dropped
    assert ex["ghost_claims"] == ["ghost9"]
    gaps = svc.read_project(USER, task)["pipeline"]["known_gaps"]
    assert any(g.get("claim_id") == "ghost9" for g in gaps)
    assert any(g.get("question", "").startswith("Does fat") for g in gaps)  # open → gap
    art = svc.read_artifact(USER, task, artifact_id="explain.md")
    assert "micellar uptake" in art["content"]


async def test_explain_invalid_after_repair_degrades_and_advances(env, monkeypatch):
    svc, task, rid = await _task(env, "EXPLAIN", question="q?", claims=[("k1", "x")])
    seen = _seam(monkeypatch, ['{"explanations": "nope"}', '{"explanations": "nope"}'])
    out = await _run(svc, task, rid)
    # EXPLAIN is degradable (unlike WRITE): ledger + honest advance, ≤2 calls
    assert out.kind == "advanced"
    assert len(seen) == 2 and _budget(svc, task)["calls"] == 2
    assert out.ledger[-1]["error_class"] == "degraded_decision"


# ═════════════════════════════ WRITE (thinking on, hard gate) ════════════════

def _good_draft() -> str:
    sec = "lycopene bioavailability evidence: heat disrupts chromoplast matrices "
    return (
        "Intro paragraph establishing the question and the honest record: "
        "verdicts, tickets and gaps are reported exactly as adjudicated.\n"
        + "\n".join(f"## Section {i}\n{sec * 4}" for i in range(4))
    )


async def test_write_normal_path_one_call_and_python_side_landing(env, monkeypatch):
    assert PIPE_CONTRACTS["WRITE"].thinking is True     # THE thinking-on node
    svc, task, rid = await _task(
        env, "WRITE", question="Does home cooking raise lycopene?",
        claims=[("k1", "cooking raises lycopene")],
        pipeline_seed={"known_gaps": [{"stage": "EVIDENCE", "claim_id": "k9",
                                       "reason": "insufficient"}]},
    )
    reply = json.dumps({"title": "Cooking and lycopene", "md": _good_draft()})
    seen = _seam(monkeypatch, [reply])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "REVIEW"
    assert len(seen) == 1 and _budget(svc, task)["calls"] == 1       # HARD METRIC
    art = svc.read_artifact(USER, task, artifact_id="report.md")     # Python-side v1
    assert art["version"] == 1 and art["status"] == "DRAFT"
    v1 = svc._load_json(svc._artifact_dir(USER, task, "report.md") / "v1", None)
    assert v1["generated_by_execution"] == f"{rid}:1:1"               # audit identity
    # G1 is satisfied by deterministic landing alone: the report-named write bound
    # the primary artifact before the force-advance ever asked the gate.
    assert svc.read_project(USER, task)["primary_report_artifact_id"] == "report.md"
    graph = svc._load_graph(USER, task)
    assert any(n.get("type") == "Draft" for n in graph["nodes"])      # graph update
    assert svc.read_project(USER, task)["pipeline"]["write"]["artifact"] == "report.md"


async def test_write_both_drafts_invalid_is_structural_terminal(env, monkeypatch):
    svc, task, rid = await _task(env, "WRITE", question="Does home cooking raise lycopene?",
                                 claims=[("k1", "x")])
    bad = json.dumps({"title": "t", "md": "## only one section"})
    seen = _seam(monkeypatch, [bad, bad])
    out = await _run(svc, task, rid)
    # repair once (≤2 calls), then STRUCTURAL: no honest draft = no completable run
    assert len(seen) == 2 and _budget(svc, task)["calls"] == 2
    assert out.kind == "blocked"
    assert out.structural["missing"] == "draft"
    assert svc.read_project(USER, task)["stage"] == "WRITE"          # never advanced
    flag = svc.read_project(USER, task)["pipeline"]["structural_stop"]
    assert flag["stage"] == "WRITE" and flag["missing"] == "draft"


async def test_write_without_question_structural_zero_llm(env, monkeypatch):
    svc, task, rid = await _task(env, "WRITE")
    seen = _seam(monkeypatch, ["{}"])
    out = await _run(svc, task, rid)
    assert out.kind == "blocked" and out.structural["missing"] == "research_question"
    assert seen == [] and _budget(svc, task)["calls"] == 0
