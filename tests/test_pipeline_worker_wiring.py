"""Worker wiring proof: the auto-run is the pipeline, and the kernel is GONE.

Batch-3 hard assertions:

* one job = exactly ONE ``pipeline.run_node`` call inside the driver lease —
  with the fenced ``execution_id`` READ BACK from the committed ledger (never a
  divergent re-mint), the configured cost cap, and the run's cumulative spend;
* ``get_agent_kernel`` / ``ReactLoopAgent`` construction on this path is
  STRICTLY ZERO (both rigged to raise); the Chat path keeps them untouched —
  its entry point is proven still present and still kernel-bound;
* the rag deployment channel is really injected through ``extras`` (the closed
  Batch-1 leftover): a down retrieval stack folds into an honest
  ``source_unavailable`` ledger line while the node still advances;
* the retired auto-run tool-surface (``_research_visible_tools``) no longer
  exists, and the workflow definition names the pipeline executor.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

import apps.worker.tasks as tasks
import plugins.research.handlers  # noqa: F401 — side effect: registers all 10 nodes
import plugins.research.pipeline as pipeline
import plugins.research.plugin as rplugin
from agent import Context, PluginManager, SkillRegistry, ToolRuntime
from plugins.research.driver import auto_turn_prompt
from plugins.research.pipeline import NodeOutcome
from plugins.research.plugin import ResearchService, register_research_plugins

USER = uuid.uuid4()


class _FakeJobStore:
    def __init__(self):
        self.running: list = []
        self.succeeded: list = []

    async def mark_running(self, job_id, error=None):
        self.running.append(job_id)

    async def mark_succeeded(self, job_id, result):
        self.succeeded.append((job_id, result))

    async def mark_failed(self, job_id, error):
        raise AssertionError(f"job marked FAILED: {error}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tests._drive_fakes import make_drive

    drive = make_drive(tmp_path)
    ctx = Context()
    ctx.provide("drive", drive)
    ctx.provide("research_scratch", tmp_path / "scratch")
    runtime = ToolRuntime()
    manager = PluginManager(runtime, SkillRegistry(), ctx)
    register_research_plugins(manager, ctx)

    service = ResearchService(drive=drive, scratch_root=tmp_path / "scratch")
    # Pin every worker surface the job touches EXCEPT the pipeline run itself.
    monkeypatch.setattr(tasks, "get_drive_service", lambda: drive)
    monkeypatch.setattr(tasks.settings, "research_scratch_dir", tmp_path / "scratch")
    # DB channel resolution: no DB route -> the pinned payload channel is used.
    from apps.api.routers import _shared

    async def _no_channel(session_factory, user_id):
        return (None, None, None, None, None)

    monkeypatch.setattr(_shared, "resolve_channel_for_owner", _no_channel)
    # Approvals: offline in-memory broker.
    import agent.security.approvals as approvals

    monkeypatch.setattr(
        approvals, "get_approval_bridge",
        lambda: SimpleNamespace(broker=None),
    )
    # Kernel spy for the Chat-path-free assertions below (auto-run must never hit it).
    monkeypatch.setattr(
        tasks, "get_agent_kernel",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("auto-run constructed the agent kernel")
        ),
    )
    return SimpleNamespace(drive=drive, service=service, ctx_obj=ctx,
                           runtime=runtime, tmp=tmp_path)


async def _make_task(env, stage: str = "DISCOVER"):
    svc = env.service
    task = (await svc.create_task(
        USER, title="tomato auto-run", execution_mode="progressive",
    ))["task_id"]
    rid = svc.begin_run(USER, task)["run_id"]
    base = svc.get_driver_checkpoint(USER, task)
    svc.atomic_update_project(
        USER, task,
        lambda p: p.__setitem__(
            "driver", {**base, "run_id": rid, "execution_id": f"{rid}:7:2",
                       "cumulative_cost_usd": 0.12},
        ),
    )
    if stage != "DISCOVER":
        svc.atomic_update_project(USER, task, lambda p: p.__setitem__("stage", stage))
    return task, rid


class _SpyDriver:
    """Captures the auto_turn call and drives the injected run_turn once,
    then returns a DROPPED-style outcome so settle short-circuits."""

    def __init__(self):
        self.calls: list[dict] = []
        self.prompt: str | None = None

    async def auto_turn(self, service, *, owner_id, task_id, run_id,
                        turn_index, run_turn):
        project = service.read_project(owner_id, task_id)
        prompt = auto_turn_prompt(
            task_name=project.get("name", task_id), project_id=task_id,
            stage=project.get("stage", "DISCOVER"), turn_index=turn_index,
        )
        res = await run_turn(prompt)
        self.prompt = res.final_answer
        self.calls.append({"run_id": run_id, "turn_index": turn_index})
        return SimpleNamespace(
            dropped=True, action="drop", reason="test short-circuit",
            final_answer=None, turn_index=turn_index,
        )


def _ctx(env):
    return {"session_factory": None, "redis": None, "job_store": _FakeJobStore(),
            "job_try": 1}


async def _drive(env, task, rid):
    job_id = str(uuid.uuid4())
    return await tasks.research_drive(
        _ctx(env), job_id,
        {"user_id": str(USER), "task_id": task, "run_id": rid, "turn_index": 7},
    )


async def test_job_runs_exactly_one_pipeline_node_never_the_kernel(env, monkeypatch):
    task, rid = await _make_task(env)
    sd = _SpyDriver()
    monkeypatch.setattr("plugins.research.driver.ResearchRunDriver", lambda *a, **k: sd)
    seen: list[dict] = []

    async def spy_node(service, owner_id, project_id, **kw):
        seen.append({"service": service, "owner": owner_id, "pid": project_id, **kw})
        stage = service.read_project(owner_id, project_id).get("stage")
        return NodeOutcome(
            "advanced", stage, "FRAME", "PIPELINE DISCOVER -> FRAME: ADVANCED",
            0.0, [],
        )

    monkeypatch.setattr(pipeline, "run_node", spy_node)
    # ReactLoopAgent rigged too: any construction on this path fails the test.
    import agent.engine as engine_mod

    monkeypatch.setattr(
        engine_mod, "ReactLoopAgent",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("auto-run constructed ReactLoopAgent")
        ),
        raising=False,
    )

    result = await _drive(env, task, rid)

    # exactly ONE node call for the whole job
    assert len(seen) == 1
    call = seen[0]
    assert call["pid"] == task and str(call["owner"]) == str(USER)
    # the FENCED identity is the one the driver's compose_prompt committed
    assert call["execution_id"] == f"{rid}:7:2"
    assert call["turn_index"] == 7 and call["run_id"] == rid
    # deployment surfaces really injected (Batch-1 leftover closed here)
    assert callable(call["extras"]["channel_rag"])
    # run-level budget wired from config + ledger spend
    assert call["max_cost_usd"] == pytest.approx(float(tasks.settings.research_driver_max_cost_usd))
    assert call["start_spent_usd"] == pytest.approx(0.12)
    # the driver saw the node's honest value as this attempt's answer
    assert sd.prompt == "PIPELINE DISCOVER -> FRAME: ADVANCED"
    assert result["dropped"] is True           # settle honored the graded outcome
    # the retired auto-run tool surface is gone
    assert not hasattr(tasks, "_research_visible_tools")


async def test_run_turn_falls_back_to_attempt_identity_when_ledger_empty(env, monkeypatch):
    task, rid = await _make_task(env)
    # clear the committed execution_id (e.g. checkpoint predates compose_prompt CAS)
    env.service.atomic_update_project(
        USER, task,
        lambda p: p["driver"].update(execution_id=None),
    )
    sd = _SpyDriver()
    monkeypatch.setattr("plugins.research.driver.ResearchRunDriver", lambda *a, **k: sd)
    seen: list[dict] = []

    async def spy_node(service, owner_id, project_id, **kw):
        seen.append(kw)
        return NodeOutcome("advanced", "DISCOVER", "FRAME", "ok", 0.0, [])

    monkeypatch.setattr(pipeline, "run_node", spy_node)
    await _drive(env, task, rid)
    assert seen[0]["execution_id"] == f"{rid}:7:1"   # deterministic attempt-1 fallback


async def test_rag_channel_wired_real_discover_degrades_kernel_absence(env, monkeypatch):
    """Full-stack pass: the REAL run_node + REAL handlers + REAL service run the
    DISCOVER node. The retrieval seam is down (get_agent_kernel rigged to raise
    in this env) — the injected channel must fold that into an honest
    source_unavailable ledger line and the node must still ADVANCE to FRAME."""
    import httpx
    from core.infrastructure.web_fetch import canonical_url as canonicalize

    def _article(fact: str) -> str:
        paras = "".join(
            f"<p>{fact} sentence {i}: cleaned article text clears the usable floor.</p>"
            for i in range(12)
        )
        return (f"<html><head><title>{fact}</title></head><body>"
                f"<nav>sidebar</nav>{paras}</body></html>")

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host.startswith("good") and host.endswith(".example"):
            return httpx.Response(200, text=_article(f"fact-{host}"))
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(rplugin, "_FETCH_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    monkeypatch.setattr(rplugin, "_FETCH_RESOLVER_OVERRIDE", lambda host: ["93.184.216.34"])

    task, rid = await _make_task(env)

    replies: list[str] = []

    async def decide(prompt: str, system: str) -> str:
        replies.append(prompt)
        return json.dumps({"keep": [canonicalize("https://good1.example/a")],
                           "notes": "usable"})

    monkeypatch.setattr(pipeline, "PIPELINE_LLM_CALL", decide)

    # Production web provider is unconfigured offline (it would itself degrade to
    # source_unavailable). Stub the DEFAULT channel so DISCOVER has one fetchable
    # candidate and the triage call runs. The worker's OWN run_turn + factory are
    # NOT touched: it injects the real channel_rag closure, whose retrieval seam
    # is the boom'd get_agent_kernel from the env fixture -> honest degrade line.
    async def web_default(q):
        return [{"url": "https://good1.example/a", "title": "G1", "text": "snippet"}]

    monkeypatch.setattr("plugins.research.handlers._web_channel", web_default)

    sd = _SpyDriver()
    monkeypatch.setattr("plugins.research.driver.ResearchRunDriver", lambda *a, **k: sd)

    await _drive(env, task, rid)

    proj = env.service.read_project(USER, task)
    assert proj["stage"] == "FRAME"                     # honest advance survived
    # the spy driver ran the WORKER'S real run_turn (not a test stand-in)
    assert sd.prompt is not None and sd.prompt.startswith("PIPELINE DISCOVER -> FRAME")
    assert "ADVANCED" in sd.prompt
    led = proj["pipeline"]["failure_ledger"]
    rag_lines = [e for e in led if e["missing"] == "rag"]
    assert rag_lines and "retrieval unavailable" in rag_lines[0]["detail"]
    assert len(replies) == 1                            # the ONE triage call ran
    corpus = proj["pipeline"]["corpus"]
    assert corpus["urls"] == [canonicalize("https://good1.example/a")]


async def test_chat_path_still_owns_the_kernel(env, monkeypatch):
    """The Chat entry point is untouched: run_agent_turn still exists and its
    source still binds get_agent_kernel (we do NOT run it — no DB in tests)."""
    import inspect

    assert callable(tasks.run_agent_turn)
    src = inspect.getsource(tasks.run_agent_turn)
    assert "get_agent_kernel" in src


def test_workflow_definition_names_the_pipeline_executor():
    from plugins.research.workflow_spec import RESEARCH_WORKFLOW, RESEARCH_EXECUTOR_ID

    assert RESEARCH_EXECUTOR_ID == "research-pipeline"
    acts = RESEARCH_WORKFLOW.spec()["activities"]
    assert acts[0]["executor"] == "research-pipeline"
    # and it is genuinely a definition change vs the retired kernel id
    assert "research-agent-kernel" not in json.dumps(acts)
