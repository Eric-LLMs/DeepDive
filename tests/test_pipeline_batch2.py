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
                claims=(), pipeline_seed: dict | None = None,
                mode: str = "progressive", description: str = ""):
    svc = ResearchService(drive=env.drive, scratch_root=env.scratch)
    # The pipeline deployment historically auto-ran PROGRESSIVE: the guard gates'
    # deterministic checks still run and their failures land in diagnostics —
    # only the blocking consequence moves to the business layer, where the
    # pipeline expresses structural inability via StructuralStop. G1 (a report
    # must exist on disk to enter REVIEW) still binds, satisfied by WRITE's
    # deterministic report.md landing below. STRICT tasks (gate-wiring tests)
    # flip this to exercise the fence auto-check + override park.
    task = (await svc.create_task(
        USER, title="tomato research", description=description,
        execution_mode=mode,
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
        "register": "recorded claim statements scored against fetched corpus text",
        "estimand": "the aggregate verdict distribution answering the question",
        "identification": "a claim is supported only when a verdict links it to evidence",
        "risk": "corpus coverage gaps and single-source claims drive the outcome",
    })
    seen = _seam(monkeypatch, [reply])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "EXECUTE"
    assert len(seen) == 1 and _budget(svc, task)["calls"] == 1      # HARD METRIC
    assert out.ledger == []
    art = svc.read_artifact(USER, task, artifact_id="design.md")
    assert "## Success criteria" in art["content"]
    assert svc.read_project(USER, task)["pipeline"]["design"]["steps"]
    # DESIGN_GATE's epistemic four fields now ride the graph Design node the
    # handler records (the gate can no longer guard a transition the pipeline
    # cannot itself satisfy).
    d = next(n for n in svc._load_graph(USER, task)["nodes"]
             if n.get("type") == "Design")
    assert all(d.get(f) for f in ("register", "estimand", "identification", "risk"))


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
    art = svc.read_artifact(USER, task, artifact_id="execution_notes.md")
    assert "2 claims, none ticketed" in art["content"]
    # the execution log must never win the primary binding (run-17/18 hijack):
    assert svc.read_project(USER, task).get("primary_report_artifact_id") != \
        "execution_notes.md"


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


# ── creation-time brief must reach every generation prompt ────────────────────
#
# Root cause of the Run-19/20 English-paper defect: task_spec.json (title +
# description) was persisted and UI-visible but read by NOTHING in the LLM
# path — the entry path (Run button, driver replay, chat) never loaded it into
# the node context. These tests pin the fix: the pipeline injects the brief
# into ctx.facts for EVERY node, and the WRITE prompt carries the verbatim
# instruction plus the output-language directive.

async def test_brief_injected_into_prompt_and_directive_present(env, monkeypatch):
    svc, task, rid = await _task(
        env, "WRITE", question="西红柿怎么做最好吃？",
        claims=[("k1", "cooking raises lycopene")],
        description="写一个中文报告",
    )
    reply = json.dumps({"title": "西红柿研究报告", "md": _good_draft()})
    seen = _seam(monkeypatch, [reply])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced"
    prompt = seen[0]
    assert "写一个中文报告" in prompt                    # verbatim user instruction
    assert "User brief" in prompt                      # block marker
    assert "output language" in prompt                 # language directive
    assert '"user_brief"' in prompt                    # structured record too
    assert "tomato research" in prompt                 # title included


async def test_no_brief_prompt_stays_at_baseline(env, monkeypatch):
    svc, task, rid = await _task(
        env, "WRITE", question="Does home cooking raise lycopene?",
        claims=[("k1", "x")],
    )
    # legacy task without a creation brief at all: blank the task_spec on disk
    (svc._project_dir(USER, task) / "task_spec.json").write_text("{}", encoding="utf-8")
    reply = json.dumps({"title": "t", "md": _good_draft()})
    seen = _seam(monkeypatch, [reply])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced"
    assert "User brief" not in seen[0]                 # no block: pre-brief baseline
    assert '"user_brief": {}' in seen[0]               # record key present, empty


async def test_read_task_spec_missing_safe(env):
    svc = ResearchService(drive=env.drive, scratch_root=env.scratch)
    assert svc.read_task_spec(USER, str(uuid.uuid4())) == {}   # unknown project


# ── and the driver path (not chat) is where the Run button executes ───────────


async def test_brief_reaches_frame_prompt_via_run_node(env, monkeypatch):
    # FRAME is the first LLM node on the Run-button path: if the brief is missing
    # here, the question gets minted in the wrong language and WRITE inherits it.
    svc, task, rid = await _task(env, "FRAME", description="写一个中文报告")
    cu = "https://good1.example/a"
    await svc.write_scratch(
        USER, task, artifact_id="corpus.md",
        content="# Research corpus\n\n## G1\nSource: " + cu + "\n\nlycopene facts.",
    )
    svc.atomic_update_project(
        USER, task,
        lambda p: p.setdefault("pipeline", {})
        .__setitem__("corpus", {"query": "tomatoes", "urls": [cu]}),
    )
    reply = json.dumps({
        "question": "home cooking lycopene bioavailability in tomatoes?",
        "in_scope": "cooking", "out_of_scope": "marketing", "claims": [],
    })
    seen = _seam(monkeypatch, [reply])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced"
    assert "写一个中文报告" in seen[0] and "User brief" in seen[0]


# ════════════ STRICT gate wiring: fence auto-check, override park, resume ═════
#
# The strict contract: an unevaluated guard gate is MECHANICALLY evaluated at
# the pipeline's transition fence (0 LLM); a checked-and-FAILED gate blocks the
# transition and parks the chain on a PENDING human override — re-entry runs no
# handler body and no LLM until a decision lands, and a resolved override
# advances WITHOUT replaying the completed node. Progressive must be untouched.

_DESIGN_8 = json.dumps({
    "method": "Adjudicate each claim against the corpus and aggregate verdicts.",
    "steps": ["claim_stats", "coverage_report"],
    "data_needed": ["corpus", "claim graph"],
    "success_criteria": "every claim carries a verdict or an honest gap",
    "register": "recorded claim statements scored against the fetched corpus",
    "estimand": "the aggregate verdict distribution answering the question",
    "identification": "a claim counts only when a verdict links it to evidence",
    "risk": "corpus coverage gaps and single-source claims drive the result",
})


async def test_strict_fence_evaluates_design_gate(env, monkeypatch):
    svc, task, rid = await _task(
        env, "DESIGN", mode="strict",
        question="Does home cooking increase lycopene bioavailability?",
        claims=[("k1", "cooking raises lycopene")],
    )
    seen = _seam(monkeypatch, [_DESIGN_8])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "EXECUTE"
    assert len(seen) == 1                                  # gate check: 0 LLM
    proj = svc.read_project(USER, task)
    assert proj["gates"]["DESIGN_GATE"] == "PASS"          # evaluated at the fence
    assert "awaiting_override" not in proj["pipeline"]
    # the node's own ledger never saw a transition_refused
    assert not any(e["error_class"] == "transition_refused"
                   for e in proj["pipeline"].get("failure_ledger") or [])


async def test_strict_gate_fail_parks_override_then_resume_advances(env, monkeypatch):
    svc, task, rid = await _task(
        env, "EXECUTE", mode="strict", question="q?",
        claims=[("k1", "one")],                            # no citations/Source
        pipeline_seed={"design": {"method": "m", "steps": ["claim_stats"]}},
    )
    seen = _seam(monkeypatch, [json.dumps({"steps": [{"op": "claim_stats"}]}),
                               json.dumps({"summary": "1 claim, 0 ticketed."})])
    out = await _run(svc, task, rid)
    assert out.kind == "blocked" and out.structural is None   # PARKED, not dead
    assert out.ledger[-1]["error_class"] == "gate_awaiting_override"
    proj = svc.read_project(USER, task)
    assert proj["stage"] == "EXECUTE"                         # no forged advance
    assert proj["gates"]["EVIDENCE_GATE"] == "FAIL"           # honestly evaluated
    aw = proj["pipeline"]["awaiting_override"]
    assert aw["gate"] == "EVIDENCE_GATE" and aw["target"] == "EXPLAIN"
    assert [a["id"] for a in svc.pending_overrides(USER, task)] == [aw["approval_id"]]
    assert "structural_stop" not in proj["pipeline"]          # first-and-only intact
    beats = len(seen)
    assert beats == 2

    # Re-entry while the approval is PENDING: parked BEFORE the handler — 0 LLM.
    out2 = await _run(svc, task, rid)
    assert out2.kind == "blocked" and "PARKED" in out2.turn_value
    assert len(seen) == beats and out2.ledger == [] and out2.cost_usd == 0.0

    # Human approves on the desktop card → OVERRIDE; the completed node advances
    # WITHOUT a handler replay.
    svc.resolve_override(USER, aw["approval_id"], approve=True, project_id=task)
    out3 = await _run(svc, task, rid)
    assert out3.kind == "advanced" and out3.next_stage == "EXPLAIN"
    assert len(seen) == beats                                 # never re-ran EXECUTE
    proj = svc.read_project(USER, task)
    assert "awaiting_override" not in proj["pipeline"]
    assert proj["stage"] == "EXPLAIN"


async def test_strict_reject_after_park_then_reject_path_keeps_structural(env, monkeypatch):
    # A human REJECTS the override: the gate stays FAIL, the chain stays parked
    # (no death flag, no LLM) — the run terminalizes honestly at the approval.
    svc, task, rid = await _task(
        env, "EXECUTE", mode="strict", question="q?", claims=[("k1", "one")],
        pipeline_seed={"design": {"method": "m", "steps": ["claim_stats"]}},
    )
    _seam(monkeypatch, [json.dumps({"steps": [{"op": "claim_stats"}]}),
                        json.dumps({"summary": "s"})])
    out = await _run(svc, task, rid)
    aw = svc.read_project(USER, task)["pipeline"]["awaiting_override"]
    svc.resolve_override(USER, aw["approval_id"], approve=False, project_id=task)
    out2 = await _run(svc, task, rid)
    assert out2.kind == "blocked" and "PARKED" in out2.turn_value
    proj = svc.read_project(USER, task)
    assert proj["stage"] == "EXECUTE"
    assert "awaiting_override" in proj["pipeline"]            # still parked, honest


async def test_strict_restart_keeps_approved_override_then_resume_advances(env, monkeypatch):
    # Incident pin: the human approves the parked override, then clicks Run before
    # the parked re-entry consumed the marker. begin_run's restart used to wipe the
    # OVERRIDE chip to NOT_RUN while ``awaiting_override`` still referenced the
    # APPROVED approval — every following turn parked at 0 LLM forever (deadlock).
    svc, task, rid = await _task(
        env, "EXECUTE", mode="strict", question="q?", claims=[("k1", "one")],
        pipeline_seed={"design": {"method": "m", "steps": ["claim_stats"]}},
    )
    seen = _seam(monkeypatch, [json.dumps({"steps": [{"op": "claim_stats"}]}),
                               json.dumps({"summary": "s"})])
    out = await _run(svc, task, rid)
    aw = svc.read_project(USER, task)["pipeline"]["awaiting_override"]
    assert aw["gate"] == "EVIDENCE_GATE"
    # A stage-entry snapshot exists (production: written by the ADVANCED commits).
    svc._write_stage_snapshot(USER, task, "EXECUTE")
    svc.resolve_override(USER, aw["approval_id"], approve=True, project_id=task)

    svc.end_run(USER, task)                                      # stalled run terminalized
    run2 = svc.begin_run(USER, task)                             # the Run click
    base2 = svc.get_driver_checkpoint(USER, task)

    def _bind(p: dict) -> None:                                  # driver binds the turn
        p["driver"] = {**base2, "run_id": run2["run_id"],
                       "execution_id": f"{run2['run_id']}:1:1"}

    svc.atomic_update_project(USER, task, _bind)
    proj = svc.read_project(USER, task)
    assert proj["gates"]["EVIDENCE_GATE"] == "OVERRIDE"          # verdict survives
    assert "awaiting_override" in proj["pipeline"]               # marker for re-entry

    out2 = await pipeline.run_node(
        svc, USER, task, run_id=run2["run_id"],
        execution_id=f"{run2['run_id']}:1:1", turn_index=1,
    )
    assert out2.kind == "advanced" and out2.next_stage == "EXPLAIN"
    proj = svc.read_project(USER, task)
    assert "awaiting_override" not in proj["pipeline"]
    assert proj["gates"]["EVIDENCE_GATE"] == "OVERRIDE"
    assert len(seen) == 2                                        # no EXECUTE replay


async def test_strict_stale_notrun_chip_self_heals_from_ledger(env, monkeypatch):
    # Legacy-state pin: data written by the buggy restart (chip NOT_RUN, approval
    # APPROVED). run_node consults the ledger, restores OVERRIDE, and advances.
    svc, task, rid = await _task(
        env, "EXECUTE", mode="strict", question="q?", claims=[("k1", "one")],
        pipeline_seed={"design": {"method": "m", "steps": ["claim_stats"]}},
    )
    _seam(monkeypatch, [json.dumps({"steps": [{"op": "claim_stats"}]}),
                        json.dumps({"summary": "s"})])
    out = await _run(svc, task, rid)
    aw = svc.read_project(USER, task)["pipeline"]["awaiting_override"]
    svc.resolve_override(USER, aw["approval_id"], approve=True, project_id=task)

    def _wipe(p: dict) -> None:                                  # simulate old begin_run
        p["gates"]["EVIDENCE_GATE"] = "NOT_RUN"

    svc.atomic_update_project(USER, task, _wipe)
    out2 = await _run(svc, task, rid)
    assert out2.kind == "advanced" and out2.next_stage == "EXPLAIN"
    proj = svc.read_project(USER, task)
    assert proj["gates"]["EVIDENCE_GATE"] == "OVERRIDE"          # healed verdict kept
    assert "awaiting_override" not in proj["pipeline"]


async def test_progressive_fence_never_evaluates_or_parks(env, monkeypatch):
    svc, task, rid = await _task(
        env, "EXECUTE", mode="progressive", question="q?",
        claims=[("k1", "one")],
        pipeline_seed={"design": {"method": "m", "steps": ["claim_stats"]}},
    )
    seen = _seam(monkeypatch, [json.dumps({"steps": [{"op": "claim_stats"}]}),
                               json.dumps({"summary": "s"})])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "EXPLAIN"
    proj = svc.read_project(USER, task)
    assert proj["gates"]["EVIDENCE_GATE"] == "NOT_RUN"     # never evaluated there
    assert "awaiting_override" not in proj.get("pipeline", {})
    assert not svc.pending_overrides(USER, task)
    assert len(seen) == 2                                  # unchanged quota
