"""Phase-2 integration: ArtifactCompileService over a real ResearchService +
the PUBLISH pdf sibling branch (docs/19 §10, inv. 9/11).

Doctrine pinned here:

* success rides the REAL typst CLI (no mock, no weakened preflight) — the run
  terminalizes COMPLETED, the drive holds real ``application/pdf`` bytes, the
  scratch mirror exists, and the persisted provenance reloads byte-equal;
* every hard fault (preflight / typst / empty graph) terminalizes
  FAILED_BLOCKED honestly and NEVER touches the drive;
* idempotency: the same manuscript replays the same committed ArtifactRef;
* the publish gate stays the sole authority: default (flag absent) behavior is
  the pre-PDF behavior exactly, and an opt-in PDF failure is a ledger
  note — never an un-promote, never a StructuralStop.
"""
from __future__ import annotations

import hashlib
import shutil
import uuid
from types import SimpleNamespace

import pytest
from agent import Context, PluginManager, SkillRegistry, ToolRuntime
from artifact_compiler.mapping import graph_evidence, provenance_map
from artifact_compiler.runstore import RunNotFound, RunStore
from core.infrastructure.request_context import set_request_user

import plugins.research.handlers  # noqa: F401 — registers the 10 nodes
from plugins.artifact.plugin import build_artifact_plugin, register_artifact_plugins
from plugins.artifact.service import ArtifactCompileError, ArtifactCompileService
from plugins.research import pipeline
from plugins.research.plugin import ResearchService, register_research_plugins

USER = uuid.uuid4()
OTHER = uuid.uuid4()

TYPST = shutil.which("typst")
requires_typst = pytest.mark.skipif(TYPST is None, reason="needs the real typst CLI")

MANUSCRIPT = (
    "# Tomato Study\n\n"
    "## Section A\n"
    "Cooking raises lycopene levels significantly [ev:bbbb].\n\n"
    "## Section B\n"
    "Raw intake absorbs poorly. Details at https://example.org/tomato.\n"
)

GRAPH = {
    "nodes": {
        "src:aaaa": {
            "id": "src:aaaa", "type": "Source",
            "url": "https://example.org/tomato", "label": "Tomato study",
        },
        "ev:bbbb": {
            "id": "ev:bbbb", "type": "Evidence",
            "excerpt": "Cooking raises lycopene levels significantly.",
        },
    },
    "edges": [{"src": "ev:bbbb", "dst": "src:aaaa", "kind": "depends_on"}],
}


@pytest.fixture(autouse=True)
def _request_user():
    set_request_user(USER)
    yield
    set_request_user(None)


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
    register_artifact_plugins(manager, ctx)
    return SimpleNamespace(
        ctx=ctx, drive=drive, manager=manager,
        scratch=tmp_path / "scratch", runs=tmp_path / "artifact_runs",
    )


async def _ready_project(env, *, with_graph: bool = True, with_report: bool = True,
                         pdf_report: bool = False, primary: bool = True) -> str:
    """A PUBLISH-ready edition: report.md v1 drafted, graph seeded, primary bound."""
    svc = ResearchService(drive=env.drive, scratch_root=env.scratch)
    task = (await svc.create_task(
        USER, title="tomato research", execution_mode="progressive",
    ))["task_id"]
    rid = svc.begin_run(USER, task)["run_id"]
    base = svc.get_driver_checkpoint(USER, task)

    def _seed(p):
        p["driver"] = {**base, "run_id": rid, "execution_id": f"{rid}:1:1"}
        p["stage"] = "PUBLISH"
        p["research_question"] = "does cooking raise lycopene?"
        p["primary_report_artifact_id"] = "report.md" if primary else ""
        if pdf_report:
            p["pdf_report"] = True
    svc.atomic_update_project(USER, task, _seed)
    if with_report:
        await svc.write_scratch(USER, task, artifact_id="report.md", content=MANUSCRIPT)
    if not primary:
        # write_scratch's T3 force-bind is undone last: the "nothing bound" case
        svc.atomic_update_project(
            USER, task, lambda p: p.__setitem__("primary_report_artifact_id", ""),
        )
    if with_graph:
        svc._save_json(svc._project_dir(USER, task) / "graph.json", GRAPH)
    env.svc = svc
    env.task = task
    return task


def _service(env) -> ArtifactCompileService:
    return ArtifactCompileService(
        research_service=env.svc, runs_root=env.runs,
    )


def _proj(env):
    return env.svc.read_project(USER, env.task)


def _run_publish(env):
    driver = _proj(env)["driver"]
    return pipeline.run_node(
        env.svc, USER, env.task,
        run_id=driver["run_id"], execution_id=driver["execution_id"], turn_index=1,
    )


# ═════════════════════════════ service: success path (real typst) ═════════════

@requires_typst
async def test_compile_real_typst_produces_promoted_pdf(env):
    task = await _ready_project(env)
    ref = await _service(env).compile_project_pdf(USER, task)

    assert ref["state"] == "completed"
    assert ref["mime_type"] == "application/pdf"
    assert ref["project_id"] == task
    assert ref["artifact_id"] == "report.md@pdf"
    assert ref["published_from"] == {"artifact_id": "report.md", "version": 1}
    assert len(ref["pdf_sha256"]) == 64 and ref["pdf_size_bytes"] > 1000
    assert "outputs/report.pdf" == ref["outputs_relative_path"]  # relative, never host path

    # real bytes, in BOTH homes: scratch mirror + drive asset row
    mirror = env.svc._project_dir(USER, task) / "outputs" / "report.pdf"
    assert mirror.is_file() and mirror.read_bytes().startswith(b"%PDF")
    asset = await env.drive.assets.get(uuid.UUID(ref["drive_asset_id"]))
    assert asset is not None and asset.mime_type == "application/pdf"
    assert hashlib.sha256(mirror.read_bytes()).hexdigest() == ref["pdf_sha256"]

    # terminal state + publishable discipline
    store = RunStore(env.runs)
    state = store.get_run(ref["run_id"])
    assert state["state"] == "completed"
    assert RunStore.is_publishable(state)


@requires_typst
async def test_provenance_survives_reload(env):
    task = await _ready_project(env)
    ref = await _service(env).compile_project_pdf(USER, task)
    store = RunStore(env.runs)  # fresh reader: only the bytes on disk
    doc = store.get_document(ref["run_id"], "provenance.json")
    assert doc == provenance_map(graph_evidence(GRAPH))
    assert doc["evidence"]["ev:bbbb"]["graph_node_id"] == "ev:bbbb"
    assert doc["evidence"]["ev:bbbb"]["source_node_id"] == "src:aaaa"
    ms = store.get_document(ref["run_id"], "manuscript.json")
    assert ms["sha256"] == hashlib.sha256(MANUSCRIPT.encode("utf-8")).hexdigest()


@requires_typst
async def test_replay_is_idempotent(env):
    task = await _ready_project(env)
    svc = _service(env)
    first = await svc.compile_project_pdf(USER, task)
    second = await svc.compile_project_pdf(USER, task)
    assert first == second  # COMPLETED replays the committed ArtifactRef verbatim


@requires_typst
async def test_status_owner_scoped(env):
    task = await _ready_project(env)
    ref = await _service(env).compile_project_pdf(USER, task)
    snap = _service(env).status(USER, ref["run_id"])
    assert snap["state"] == "completed" and snap["artifact_ref"] == ref
    with pytest.raises(RunNotFound):  # never leak existence to another tenant
        _service(env).status(OTHER, ref["run_id"])


# ═════════════════════════════ hard faults → FAILED_BLOCKED ═══════════════════

@requires_typst
async def test_typst_failure_terminals_blocked_and_saves_nothing(env, monkeypatch):
    task = await _ready_project(env)
    import plugins.artifact.service as svc_mod
    monkeypatch.setattr(
        svc_mod, "run_typst_compile",
        lambda *a, **k: (False, "boom: typst exploded"),
    )
    with pytest.raises(ArtifactCompileError) as exc:
        await _service(env).compile_project_pdf(USER, task)
    run_id = exc.value.run_id
    assert run_id and "typst compilation failed" in str(exc.value)
    assert RunStore(env.runs).get_run(run_id)["state"] == "failed_blocked"
    # the drive and the scratch mirror were never touched
    assert not (env.svc._project_dir(USER, task) / "outputs" / "report.pdf").exists()
    assert all(
        a.name != "report.pdf" for a in env.drive.assets.rows.values()
    )


async def test_preflight_failure_terminals_blocked(env):
    task = await _ready_project(env)
    broken = ArtifactCompileService(
        research_service=env.svc, runs_root=env.runs,
        typst_bin="definitely-not-a-typst-binary-xyz",
    )
    with pytest.raises(ArtifactCompileError) as exc:
        await broken.compile_project_pdf(USER, task)
    assert "preflight" in str(exc.value)
    assert RunStore(env.runs).get_run(exc.value.run_id)["state"] == "failed_blocked"


@requires_typst
async def test_empty_graph_refuses_unsourced_report(env):
    task = await _ready_project(env, with_graph=False)
    with pytest.raises(ArtifactCompileError) as exc:
        await _service(env).compile_project_pdf(USER, task)
    assert "no graph evidence" in str(exc.value)
    assert RunStore(env.runs).get_run(exc.value.run_id)["state"] == "failed_blocked"


async def test_no_primary_raises_before_any_run(env):
    task = await _ready_project(env, primary=False)
    with pytest.raises(ArtifactCompileError) as exc:
        await _service(env).compile_project_pdf(USER, task)
    assert exc.value.run_id is None  # nothing was created — the gate owns this verdict
    assert not env.runs.exists() or list(env.runs.glob("*/run.json")) == []


# ═════════════════════════════ plugin surface ═════════════════════════════════

def test_plugin_shape_and_tool_schema(env):
    pl = build_artifact_plugin(env.ctx)
    assert pl.name == "artifact" and pl.inject == ["drive", "research_scratch"]
    assert [t.name for t in pl.tools] == ["artifact"]
    tool = pl.tools[0]
    schema = tool.parameters
    assert schema["properties"]["action"]["enum"] == ["compile_pdf", "status"]
    assert schema["required"] == ["action"]
    # registration rode the manager without collision
    assert env.manager.get("artifact") is not None
    assert env.manager.get("research") is not None


# ═════════════════════════════ publish gate integration ════════════════════════

async def test_publish_default_unchanged_without_flag(env, monkeypatch):
    """pdf_report absent -> zero artifact code runs (gate byte-identical)."""
    import plugins.artifact.service as svc_mod

    def _trip(*a, **k):
        raise AssertionError("pdf branch entered without the opt-in flag")

    monkeypatch.setattr(svc_mod, "run_typst_compile", _trip)
    await _ready_project(env)
    out = await _run_publish(env)
    assert out.kind == "advanced" and out.next_stage is None
    pub = _proj(env)["pipeline"]["publish"]
    assert pub["status"] == "PROMOTED" and "pdf" not in pub
    assert out.ledger == []


@requires_typst
async def test_publish_with_flag_writes_pdf_sibling(env):
    task = await _ready_project(env, pdf_report=True)
    out = await _run_publish(env)
    assert out.kind == "advanced" and out.ledger == []
    pub = _proj(env)["pipeline"]["publish"]
    assert pub["status"] == "PROMOTED"          # markdown authority intact
    assert pub["pdf"]["state"] == "completed"
    assert pub["pdf"]["outputs_relative_path"] == "outputs/report.pdf"
    mirror = env.svc._project_dir(USER, task) / "outputs" / "report.pdf"
    assert mirror.is_file() and mirror.read_bytes().startswith(b"%PDF")


async def test_publish_pdf_failure_never_undoes_promotion(env, monkeypatch):
    await _ready_project(env, pdf_report=True)
    import plugins.artifact.service as svc_mod
    monkeypatch.setattr(
        svc_mod, "run_typst_compile", lambda *a, **k: (False, "boom"),
    )
    out = await _run_publish(env)
    assert out.kind == "advanced"               # the gate's verdict stands
    pub = _proj(env)["pipeline"]["publish"]
    assert pub["status"] == "PROMOTED" and "pdf" not in pub
    ledger = out.ledger
    assert any(
        e.get("error_class") == "handler_error" and "pdf compile failed" in e.get("detail", "")
        for e in ledger
    )


async def test_publish_gate_not_bypassed_by_pdf_flag(env):
    """Empty hand still blocks even with pdf_report on — the flag adds a sibling,
    never authority."""
    task = await _ready_project(env, with_report=False, pdf_report=True)
    out = await _run_publish(env)
    assert out.kind == "blocked"
    assert out.structural["stage"] == "PUBLISH"
    assert out.structural["missing"] == "promoted_artifact"
    pub = (_proj(env)["pipeline"].get("publish") or {})
    assert "pdf" not in pub
    assert not (env.svc._project_dir(USER, task) / "outputs").exists()
