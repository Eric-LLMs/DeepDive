"""Stub E2E: the full 10-stage auto-run chain through the REAL worker shape.

Harness = the batch-3 doctrine proven end-to-end with zero network and zero
real LLM:

* worker-shaped loop: each iteration = one ``ResearchRunDriver.auto_turn``
  lease whose ``run_turn`` is byte-for-byte the worker seam (execution_id read
  back from the committed ledger, config cost cap, ledger spend, the rag
  channel injected through ``extras``) calling ``pipeline.run_node``;
* STRICT stage sequence ``DISCOVER → FRAME → EVIDENCE → DESIGN → EXECUTE →
  EXPLAIN → WRITE → REVIEW → REPRODUCE → PUBLISH`` and FINISHED terminal;
* per-node quota from the persisted budget snapshots: REPRODUCE=0, PUBLISH=0,
  EXECUTE=exactly 2, every other node ≤ 2;
* the agent kernel is NEVER constructed on this path (rigged to raise);
* StructuralStop variant: the dead node terminalizes BLOCKED in the SAME
  grading pass — no next iteration, no stage change, zero same-node re-runs
  (a slipped successor short-circuits behind the re-entry guard: no handler
  body, no LLM).
"""
from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace

import httpx
import pytest

import plugins.research.handlers as handlers
import plugins.research.pipeline as pipeline
import plugins.research.plugin as rplugin
from core.config import settings
from core.infrastructure.request_context import set_request_user
from core.infrastructure.web_fetch import canonical_url as canonicalize
from plugins.research.driver import ResearchRunDriver, RunState, RunTurnResult
from plugins.research.pipeline import NodeCtx
from plugins.research.plugin import ResearchService, register_research_plugins

USER = uuid.uuid4()
_PUBLIC = "93.184.216.34"

STAGE_SEQUENCE = [
    "DISCOVER", "FRAME", "EVIDENCE", "DESIGN", "EXECUTE",
    "EXPLAIN", "WRITE", "REVIEW", "REPRODUCE", "PUBLISH",
]


@pytest.fixture(autouse=True)
def _request_user():
    set_request_user(USER)
    yield
    set_request_user(None)


@pytest.fixture(autouse=True)
def _clean_buffers():
    rplugin._PENDING_ASSET_MERGES.clear()
    yield
    rplugin._PENDING_ASSET_MERGES.clear()


@pytest.fixture(autouse=True)
def _no_kernel():
    """The pipeline auto-run must never touch the agent kernel (batch-3 mandate)."""
    import api.agent_factory as factory

    def boom(*a, **k):
        raise AssertionError("pipeline auto-run constructed the agent kernel")

    saved = factory.get_agent_kernel
    factory.get_agent_kernel = boom
    try:
        import apps.worker.tasks as tasks
        saved2 = tasks.get_agent_kernel
        tasks.get_agent_kernel = boom
        try:
            yield
        finally:
            tasks.get_agent_kernel = saved2
    finally:
        factory.get_agent_kernel = saved


@pytest.fixture
def env(tmp_path):
    from tests._drive_fakes import make_drive

    drive = make_drive(tmp_path)
    ctx = SimpleNamespace(ctx=None)
    from agent import Context, PluginManager, SkillRegistry, ToolRuntime
    c = Context()
    c.provide("drive", drive)
    c.provide("research_scratch", tmp_path / "scratch")
    runtime = ToolRuntime()
    manager = PluginManager(runtime, SkillRegistry(), c)
    register_research_plugins(manager, c)
    return SimpleNamespace(drive=drive, scratch=tmp_path / "scratch")


# ── the stub brains: route by the deterministic prompt signatures ────────────
_DRAFT_SEC = ("lycopene bioavailability evidence: heat disrupts chromoplast "
              "matrices, freeing bound lycopene for micellar uptake. ")


def _draft_md() -> str:
    return ("Intro paragraph establishing the question and the honest record: "
            "verdicts, tickets and gaps are reported exactly as adjudicated.\n"
            + "\n".join(f"## Section {i}\n{_DRAFT_SEC * 5}" for i in range(4)))


def _install_llms(monkeypatch):
    """Both LLM seams as ONE routing brain. Every prompt is matched against a
    stage-signature; unknown prompts are a harness bug (loud AssertionError)."""
    calls: list[str] = []

    async def _route(prompt: str) -> str:
        calls.append(prompt)
        if prompt.startswith("Research topic:") and "Candidate sources:" in prompt:
            return json.dumps({"keep": [canonicalize("https://good1.example/a")],
                               "notes": "one usable source"})
        if prompt.startswith("Research topic:") and "Corpus (truncated)" in prompt:
            return json.dumps({
                "question": "Does home cooking measurably increase lycopene "
                            "bioavailability in tomatoes?",
                "in_scope": "cooking methods and bioassays",
                "out_of_scope": "marketing",
                "claims": [{"id": "k1", "statement": "Cooking raises lycopene availability",
                            "strength": "medium",
                            "citations": [canonicalize("https://good1.example/a")]}],
            })
        if '"source_id": "S' in prompt:
            sids = re.findall(r'"source_id": "(S\d+)"', prompt)
            cids = sorted(set(re.findall(r'"claim_id": "(k\d+)"', prompt)))
            return json.dumps({"results": [
                {"source_id": s, "claim_id": c, "verdict": "supports"}
                for s in sids for c in cids]})
        if prompt.startswith("Research question:") and "Design the analysis method" in prompt:
            return json.dumps({
                "method": "Adjudicate each claim against the corpus and aggregate "
                          "verdict tickets per claim.",
                "steps": ["claim_stats", "coverage_report"],
                "data_needed": ["corpus", "claim graph"],
                "success_criteria": "every claim carries a verdict or an honest gap",
                "register": "recorded claim statements scored against corpus passages",
                "estimand": "the aggregate verdict distribution answering the question",
                "identification": "a claim counts only when a verdict links evidence",
                "risk": "corpus coverage gaps and single-source claims drive result",
            })
        if "Valid ops with their meaning" in prompt:
            return json.dumps({"steps": [{"op": "claim_stats"}, {"op": "coverage_report"}]})
        if prompt.startswith("Execution outputs:"):
            return json.dumps({"summary": "1 claim ticketed supports; coverage 1/1."})
        if prompt.startswith("Question:") and "Claim ids:" in prompt \
                and "Verdict tickets" in prompt:
            return json.dumps({
                "explanations": [{"claim_id": "k1",
                                  "causal_line": "heat frees bound lycopene for "
                                                 "micellar uptake.",
                                  "confidence": "medium"}],
                "open_questions": [],
            })
        if prompt.startswith("Evidence record (authoritative"):
            return json.dumps({"title": "Cooking and lycopene", "md": _draft_md()})
        if prompt.startswith("Review the draft below"):
            return '{"changes": []}'
        raise AssertionError(f"stub brain saw an UNKNOWN prompt: {prompt[:160]!r}")

    async def pipeline_seam(prompt: str, system: str) -> str:
        return await _route(prompt)

    async def adj_seam(prompt: str, system_prompt: str) -> str:
        return await _route(prompt)

    monkeypatch.setattr(pipeline, "PIPELINE_LLM_CALL", pipeline_seam)
    monkeypatch.setattr(rplugin, "_ADJ_LLM_CALL", adj_seam)
    return calls


def _install_fetch(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host.startswith("good") and host.endswith(".example"):
            n = 12
            paras = "".join(
                f"<p>fact-{host} sentence {i}: cleaned article text clears the "
                "usable floor so the page counts as a verifiable source.</p>"
                for i in range(n))
            return httpx.Response(
                200, text=f"<html><head><title>fact-{host}</title></head>"
                          f"<body><nav>sb</nav>{paras}</body></html>")
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(rplugin, "_FETCH_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    monkeypatch.setattr(rplugin, "_FETCH_RESOLVER_OVERRIDE", lambda host: [_PUBLIC])
    # Hermetic channels: the production providers are environment-dependent;
    # DISCOVER's deployment surface is injected by the harness, these defaults
    # are stubbed to the mock-transport corpus (web=2 usable, social=0).
    async def web(q):
        return [{"url": "https://good1.example/a", "title": "G1", "text": "snippet"},
                {"url": "https://good2.example/b", "title": "G2", "text": "snippet"}]

    async def none(q):
        return []

    monkeypatch.setattr(handlers, "_web_channel", web)
    monkeypatch.setattr(handlers, "_social_channel", none)


async def _make_task(env, *, mode: str = "progressive"):
    svc = ResearchService(drive=env.drive, scratch_root=env.scratch)
    task = (await svc.create_task(
        USER, title="health effects of cooking tomatoes",
        execution_mode=mode,
    ))["task_id"]
    rid = svc.begin_run(USER, task)["run_id"]
    return svc, task, rid


def _rag_channel_factory(user_id):
    """The worker seam's rag channel: kernel is rigged to raise in this suite →
    the channel degrades HONESTLY into a source_unavailable ledger line."""
    async def _ch(query: str) -> list[dict]:
        raise RuntimeError("retrieval unavailable: no kernel in stub env")
    return _ch


async def _drive_chain(env, monkeypatch, *, stop_after: int | None = None,
                       mode: str = "progressive"):
    """The worker loop shape: one auto_turn per iteration until terminal."""
    calls = _install_llms(monkeypatch)
    _install_fetch(monkeypatch)
    svc, task, rid = await _make_task(env, mode=mode)
    driver = ResearchRunDriver()

    async def run_turn(prompt: str) -> RunTurnResult:
        led = svc.get_driver_checkpoint(USER, task) or {}
        execution_id = led.get("execution_id") or f"{rid}:{led.get('turn_index', 1)}:1"
        turn_index = int(led.get("turn_index") or 1)
        out = await pipeline.run_node(
            svc, USER, task,
            run_id=rid, execution_id=execution_id, turn_index=turn_index,
            max_cost_usd=settings.research_driver_max_cost_usd,
            start_spent_usd=float(led.get("cumulative_cost_usd") or 0.0),
            extras={"channel_rag": _rag_channel_factory(USER)},
        )
        return RunTurnResult(final_answer=out.turn_value, cost_usd=out.cost_usd)

    visited: list[str] = []
    outcomes = []
    ti = 1
    while ti <= 12:
        outcome = await driver.auto_turn(
            svc, owner_id=USER, task_id=task, run_id=rid,
            turn_index=ti, run_turn=run_turn,
        )
        outcomes.append(outcome)
        visited.append(svc.read_project(USER, task).get("stage"))
        if stop_after is not None and len(outcomes) >= stop_after:
            break
        if outcome.action != "continue":
            break
        ti = outcome.next_turn_index
    return SimpleNamespace(svc=svc, task=task, rid=rid, visited=visited,
                           outcomes=outcomes, llm_prompts=calls)


# ════════════════ STRICT: the four guard gates, wired end to end ═══════════════

async def test_strict_e2e_all_four_gates_pass(env, monkeypatch):
    """The gap this pins: strict was never driven through a guarded transition.

    Direct run_node loop (the driver's park semantics are pinned in
    test_pipeline_batch2): each iteration first resolves any PENDING override
    (the desktop "Approve" click), then runs one node. The stub graph is
    genuinely complete — DESIGN node recorded, adjudicated sources verified,
    the claim cited, the report landed, the mechanical scorecard clean — so
    the expectation here is FOUR REAL PASSes and ZERO parks.
    """
    calls = _install_llms(monkeypatch)
    _install_fetch(monkeypatch)
    svc, task, rid = await _make_task(env, mode="strict")
    # this loop bypasses the driver, so nothing rotates the lease: pin the
    # on-disk driver execution_id to the constant fence the commits are checked
    # against (mirrors test_pipeline_batch2's seeding).
    _base = svc.get_driver_checkpoint(USER, task) or {}
    svc.atomic_update_project(
        USER, task,
        lambda p: p.__setitem__("driver", {**_base, "run_id": rid,
                                           "execution_id": f"{rid}:1:1"}),
    )

    visited: list[str] = ["DISCOVER"]
    parks: list[str] = []
    for ti in range(1, 25):
        for a in svc.pending_overrides(USER, task):
            parks.append(a["gate_name"])
            svc.resolve_override(USER, a["id"], approve=True, project_id=task)
        led = svc.get_driver_checkpoint(USER, task) or {}
        out = await pipeline.run_node(
            svc, USER, task, run_id=rid,
            # constant lease id (the driver would rotate it per turn; this loop
            # bypasses the driver — the F2 fence compares commits to the lease)
            execution_id=led.get("execution_id") or f"{rid}:1:1",
            turn_index=ti,
        )
        assert out.structural is None, out.turn_value
        stage_now = svc.read_project(USER, task).get("stage")
        if visited[-1] != stage_now:
            visited.append(stage_now)
        if stage_now == "PUBLISH" and out.kind == "advanced" and out.next_stage is None:
            break

    proj = svc.read_project(USER, task)
    assert parks == []
    assert visited == STAGE_SEQUENCE, visited
    g = proj["gates"]
    assert g["DESIGN_GATE"] == "PASS" and g["EVIDENCE_GATE"] == "PASS"
    assert g["CLAIM_GATE"] == "PASS" and g["QUALITY_GATE"] == "PASS"
    assert "structural_stop" not in proj["pipeline"]
    assert "awaiting_override" not in proj["pipeline"]
    pub = proj["pipeline"]["publish"]
    assert pub["status"] == "PROMOTED"
    # the promoted deliverable is the PAPER, not the execution log (run-17/18 hijack)
    assert pub["artifact"] == "report.md" == proj["primary_report_artifact_id"]
    # the mechanical scorecard the gate consumed is THIS edition's artifact
    sc = svc.read_artifact(USER, task, artifact_id="scorecard.md")
    assert sc["content"].count("\n| ") >= 8


# ═══════════════════════ the golden path: 10 stages, FINISHED ═════════════════

async def test_stub_e2e_exact_ten_stage_sequence_to_finished(env, monkeypatch):
    r = await _drive_chain(env, monkeypatch)
    # STRICT sequence, observed AFTER each node ran: every stage DISCOVER
    # advanced TO appears in DAG order, no skips, no loops, and the last two
    # iterations (REPRODUCE, PUBLISH) are terminal-self stages.
    expected = STAGE_SEQUENCE[1:] + ["PUBLISH"]
    assert r.visited == expected, r.visited
    # the executed nodes, named by each turn's value (DISCOVER..PUBLISH = 10)
    executed = [re.match(r"PIPELINE (\w+)", o.final_answer or "").group(1)
                for o in r.outcomes]
    assert executed == STAGE_SEQUENCE
    last = r.outcomes[-1]
    assert last.action == "finished" and last.state == RunState.FINISHED
    assert last.next_turn_index is None                 # nothing scheduled after
    proj = r.svc.read_project(USER, r.task)
    assert proj["stage"] == "PUBLISH"
    # the terminal gate's own proof: a promoted, drive-backed artifact
    pub = proj["pipeline"]["publish"]
    assert pub["status"] == "PROMOTED" and pub["drive_asset_id"]
    # the promoted deliverable is the PAPER, not the execution log (run-17/18
    # hijack: execution_report.md matched the report-name binder first)
    assert pub["artifact"] == "report.md" == proj["primary_report_artifact_id"]
    # the review verdict rode the whole chain honestly
    assert proj["pipeline"]["review"]["status"] == "pass"
    # and the reproducibility audit is all green
    assert all(c["ok"] for c in proj["pipeline"]["reproduce"]["checks"])
    # FINISHED means the chain never structural-stopped
    assert "structural_stop" not in (proj.get("pipeline") or {})


async def test_stub_e2e_per_node_quota(env, monkeypatch):
    # Per-stage exact budgets are pinned by the dedicated node tests
    # (test_pipeline_batch1/2/3). Here the WHOLE-CHAIN total: happy path is
    # 1(DISCOVER)+1(FRAME)+1(EVIDENCE)+1(DESIGN)+2(EXECUTE)+1(EXPLAIN)
    # +1(WRITE)+1(REVIEW)+0(REPRODUCE)+0(PUBLISH) = 9 completions, ever.
    r = await _drive_chain(env, monkeypatch)
    assert len(r.llm_prompts) == 9
    # REPRODUCE / PUBLISH contribute NOTHING: the final completion is REVIEW's.
    assert r.llm_prompts[-1].startswith("Review the draft below")


async def test_stub_e2e_execute_node_spends_exactly_two(env, monkeypatch):
    """EXECUTE quota pinned inside the live chain: the two beats are the ONLY
    prompts between the DESIGN reply and the EXPLAIN prompt."""
    r = await _drive_chain(env, monkeypatch)
    prompts = r.llm_prompts
    plan_i = next(i for i, p in enumerate(prompts) if "Valid ops with their meaning" in p)
    summ_i = plan_i + 1
    assert prompts[summ_i].startswith("Execution outputs:")
    # no third beat ever exists anywhere in the chain
    assert sum(1 for p in prompts if "Valid ops" in p or p.startswith("Execution outputs")) == 2


async def test_stub_e2e_reproduce_publish_zero_llm(env, monkeypatch):
    r = await _drive_chain(env, monkeypatch)
    # after the reviewer's reply, EXACTLY nothing more was ever completed
    review_i = max(i for i, p in enumerate(r.llm_prompts)
                   if p.startswith("Review the draft below"))
    assert review_i == len(r.llm_prompts) - 1
    # REPRODUCE and PUBLISH ran their full deterministic bodies anyway:
    proj = r.svc.read_project(USER, r.task)
    assert proj["pipeline"]["reproduce"]["checks"]
    assert proj["pipeline"]["publish"]["status"] == "PROMOTED"


async def test_stub_e2e_degradation_is_honest_not_hidden(env, monkeypatch):
    r = await _drive_chain(env, monkeypatch)
    # The rag channel was injected for real and its outage is a LEDGER line —
    # the run still finished. (Batch-1 leftover closed: worker injects it.)
    proj = r.svc.read_project(USER, r.task)
    led = proj["pipeline"]["failure_ledger"]
    assert any(e["error_class"] == "source_unavailable" and e["missing"] == "rag"
               for e in led)
    assert proj["pipeline"]["publish"]["status"] == "PROMOTED"


# ═══════════════════════ StructuralStop: BLOCKED, never re-run ════════════════

async def test_structural_stop_terminalizes_immediately_no_rerun(env, monkeypatch):
    calls = _install_llms(monkeypatch)
    _install_fetch(monkeypatch)
    svc, task, rid = await _make_task(env)
    driver = ResearchRunDriver()

    # rig WRITE: both draft attempts invalid -> StructuralStop("WRITE","draft")
    bad_draft = json.dumps({"title": "t", "md": "## only one section"})
    real_route = None
    seen_write: list[str] = []

    async def pipeline_seam(prompt: str, system: str) -> str:
        if prompt.startswith("Evidence record (authoritative"):
            seen_write.append(prompt)
            return bad_draft
        return await _routing(prompt)

    async def adj_seam(prompt: str, system_prompt: str) -> str:
        return await _routing(prompt)

    async def _routing(prompt: str) -> str:
        calls.append(prompt)
        if prompt.startswith("Research topic:") and "Candidate sources:" in prompt:
            return json.dumps({"keep": [canonicalize("https://good1.example/a")], "notes": "n"})
        if prompt.startswith("Research topic:"):
            return json.dumps({
                "question": "Does home cooking measurably increase lycopene "
                            "bioavailability in tomatoes?",
                "in_scope": "cooking", "out_of_scope": "marketing",
                "claims": [{"id": "k1", "statement": "Cooking raises lycopene availability",
                            "strength": "medium",
                            "citations": [canonicalize("https://good1.example/a")]}]})
        if '"source_id": "S' in prompt:
            sids = re.findall(r'"source_id": "(S\d+)"', prompt)
            return json.dumps({"results": [
                {"source_id": s, "claim_id": "k1", "verdict": "supports"} for s in sids]})
        if "Design the analysis method" in prompt:
            return json.dumps({
                "method": "Adjudicate each claim against the corpus and aggregate verdicts.",
                "steps": ["claim_stats"], "data_needed": ["corpus"],
                "success_criteria": "verdicts or honest gaps",
                "register": "claim statements scored against the fetched corpus text",
                "estimand": "aggregate verdict distribution as the answer",
                "identification": "verdicts link a claim to evidence or nothing",
                "risk": "corpus coverage gaps and single-source claims",
            })
        if "Valid ops with their meaning" in prompt:
            return json.dumps({"steps": [{"op": "claim_stats"}]})
        if prompt.startswith("Execution outputs:"):
            return json.dumps({"summary": "done"})
        if prompt.startswith("Question:") and "Claim ids:" in prompt:
            return json.dumps({"explanations": [
                {"claim_id": "k1", "causal_line": "heat frees lycopene.",
                 "confidence": "medium"}], "open_questions": []})
        if prompt.startswith("Review the draft below"):
            return '{"changes": []}'
        raise AssertionError(f"unexpected prompt: {prompt[:120]!r}")

    monkeypatch.setattr(pipeline, "PIPELINE_LLM_CALL", pipeline_seam)
    monkeypatch.setattr(rplugin, "_ADJ_LLM_CALL", adj_seam)

    # count WRITE handler entries: a re-run of the dead node is the exact fault
    entries: list[int] = []
    real_write = pipeline.HANDLERS["WRITE"]

    async def counting(ctx: NodeCtx) -> None:
        entries.append(1)
        await real_write(ctx)

    monkeypatch.setitem(pipeline.HANDLERS, "WRITE", counting)

    async def run_turn(prompt: str) -> RunTurnResult:
        led = svc.get_driver_checkpoint(USER, task) or {}
        out = await pipeline.run_node(
            svc, USER, task, run_id=rid,
            execution_id=led.get("execution_id") or f"{rid}:1:1",
            turn_index=int(led.get("turn_index") or 1),
            max_cost_usd=settings.research_driver_max_cost_usd,
            start_spent_usd=float(led.get("cumulative_cost_usd") or 0.0),
            extras={"channel_rag": _rag_channel_factory(USER)},
        )
        return RunTurnResult(final_answer=out.turn_value, cost_usd=out.cost_usd)

    # drive until the WRITE node stops the chain (7th stage: 6 advances + this)
    ti, outcomes = 1, []
    while ti <= 12:
        o = await driver.auto_turn(svc, owner_id=USER, task_id=task, run_id=rid,
                                   turn_index=ti, run_turn=run_turn)
        outcomes.append(o)
        if o.action != "continue":
            break
        ti = o.next_turn_index

    last = outcomes[-1]
    # BLOCKED terminal IN THE SAME grading pass — the 7th iteration is the last
    assert last.action == "blocked" and last.state == RunState.BLOCKED
    assert "structural" in (last.reason or "")
    assert getattr(last, "next_turn_index", None) is None
    # no stage change happened on the dead node
    assert svc.read_project(USER, task)["stage"] == "WRITE"
    flag = svc.read_project(USER, task)["pipeline"]["structural_stop"]
    assert flag["stage"] == "WRITE" and flag["missing"] == "draft"
    # zero same-node re-runs across the whole drive
    assert len(entries) == 1
    # WRITE was asked for EXACTLY 2 drafts (decide + one repair) and never a 3rd
    assert len(seen_write) == 2
    # hypothetical slipped successor (buggy scheduler): the re-entry guard
    # short-circuits BEFORE the handler — still no LLM, no body, no stage move.
    before = len(calls)
    o2 = await driver.auto_turn(svc, owner_id=USER, task_id=task, run_id=rid,
                                turn_index=7, run_turn=run_turn)
    assert len(entries) == 1 and len(calls) == before
    assert o2.state in (RunState.BLOCKED, RunState.ERROR, RunState.FINISHED) \
        or o2.action != "continue"
    assert svc.read_project(USER, task)["stage"] == "WRITE"
