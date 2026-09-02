"""Phase 0.5 architecture spike tests: the six research tools through the real runtime.

Proves the six frozen mechanisms against ``plugins/research/plugin.py``:
1. Cordis mounting (PENDING -> ACTIVE on capability provision; discover() skips the file).
2. Project persistence + crash recovery (on-disk state, no workflow_state.json, tenancy).
3. Three-layer storage (scratch -> cloud drive -> RAG projection trigger).
4. Graph lineage + STALE/INVALID cascade.
5. Gate override flow (FAIL -> PENDING approval -> human APPROVED -> OVERRIDE).
6. Idempotency + immutable executions (producer invariant, no duplicates).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from agent import Context, PluginManager, SkillRegistry, ToolRuntime
from agent.engine.decisions import ToolExecution
from core.application.drive_service import DriveError
from core.infrastructure.request_context import set_request_user
from plugins.research.plugin import ResearchService, build_research_plugin, register_research_plugins
from tests._drive_fakes import make_drive

USER = uuid.uuid4()
USER_B = uuid.uuid4()

RESEARCH_TOOLS = {
    "research_project",
    "research_artifact",
    "research_state",
    "research_evidence",
    "research_gate",
    "research_run",
}


@pytest.fixture(autouse=True)
def _request_user():
    set_request_user(USER)
    yield
    set_request_user(None)


@pytest.fixture
def env(tmp_path):
    """A mounted research plugin: real ToolRuntime + Context, fake drive, real scratch dir."""
    drive = make_drive(tmp_path)
    ctx = Context()
    ctx.provide("drive", drive)
    ctx.provide("research_scratch", tmp_path / "scratch")
    runtime = ToolRuntime()
    manager = PluginManager(runtime, SkillRegistry(), ctx)
    register_research_plugins(manager, ctx)
    return SimpleNamespace(
        ctx=ctx,
        drive=drive,
        runtime=runtime,
        manager=manager,
        scratch=tmp_path / "scratch",
    )


async def _run(runtime: ToolRuntime, _tool_name: str, **args) -> dict:
    result = await runtime.execute(
        ToolExecution(call_id=str(uuid.uuid4()), name=_tool_name, arguments=args)
    )
    if result.is_error:
        raise AssertionError(f"tool {_tool_name} failed: {result.error.message}")
    return result.value


async def _create_project(runtime: ToolRuntime, project_name: str = "spike", **extra) -> dict:
    return await _run(
        runtime,
        "research_project",
        **{"action": "create", "name": project_name, **extra},
    )


async def _walk_to(runtime: ToolRuntime, pid: str, stage: str) -> None:
    """Advance the state machine step-by-step through ``stage`` (gate guards honored)."""
    # Projects are born in DISCOVER, so the walk starts from the second stage onward.
    current = "DISCOVER"
    order = ["DISCOVER", "FRAME", "EVIDENCE", "DESIGN", "EXECUTE", "EXPLAIN", "WRITE"]
    for target in order[1:]:
        if order.index(target) > order.index(stage):
            break
        if target == "EXECUTE":
            # DESIGN -> EXECUTE is guarded by DESIGN_GATE.
            await _run(
                runtime,
                "research_evidence",
                action="record_node",
                project_id=pid,
                node={
                    "id": "dg",
                    "type": "Design",
                    "label": "design",
                    "register": "outcome",
                    "estimand": "ATE",
                    "identification": "conditional",
                    "risk": "confounding",
                },
            )
            assert (await _run(runtime, "research_gate", action="check", project_id=pid, gate_name="DESIGN_GATE"))["status"] == "PASS"
        res = await _run(
            runtime, "research_state", action="transition_stage", project_id=pid, target=target
        )
        assert res["granted"] is True, f"{current} -> {target}: {res.get('reason')}"
        current = target


# ── 1. Cordis mounting ────────────────────────────────────────────────────────
class TestCordisMounting:
    async def test_register_holds_pending_until_capabilities_provided(self, tmp_path):
        ctx = Context()
        runtime = ToolRuntime()
        manager = PluginManager(runtime, SkillRegistry(), ctx)
        register_research_plugins(manager, ctx)

        assert manager.pending_names() == ["research"]
        assert manager.names() == []
        assert runtime.all() == []  # not mounted yet -> no tools registered

        ctx.provide("drive", make_drive(tmp_path))
        assert manager.pending_names() == ["research"]  # research_scratch still missing

        ctx.provide("research_scratch", tmp_path / "scratch")
        assert manager.names() == ["research"]
        assert {t.name for t in runtime.all()} == RESEARCH_TOOLS

    async def test_discover_skips_factory_plugin(self):
        # No module-level PLUGIN -> discover() safely skips the research file (count 0).
        ctx = Context()
        runtime = ToolRuntime()
        manager = PluginManager(runtime, SkillRegistry(), ctx)
        count = manager.discover(__import__("pathlib").Path("plugins/research"))
        assert count == 0
        assert runtime.all() == []

    async def test_all_six_tools_registered(self, env):
        assert {t.name for t in env.runtime.all()} == RESEARCH_TOOLS
        plugin = env.manager.get("research")
        assert plugin is not None
        assert plugin.inject == ["drive", "research_scratch"]


# ── 2. Project persistence + crash recovery ───────────────────────────────────
class TestProjectPersistence:
    async def test_create_persists_project_json_without_workflow_state(self, env):
        created = await _create_project(env.runtime, name="lit review", profile="literature")
        pid = created["project_id"]
        project_dir = env.scratch / str(USER) / pid
        assert project_dir.is_dir()
        assert (project_dir / "project.json").is_file()

        # The spike must never write the legacy workflow_state.json.
        assert list(env.scratch.rglob("workflow_state.json")) == []

        project = ResearchService._load_json(project_dir / "project.json", None)
        assert project["name"] == "lit review"
        assert project["profile"] == "literature"
        assert project["stage"] == "DISCOVER"
        assert project["status"] == "ACTIVE"

    async def test_crash_resume_via_fresh_service(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        await _run(env.runtime, "research_evidence", action="record_node", project_id=pid,
                   node={"id": "S", "type": "Source", "label": "source"})

        # Simulate a crash: a brand-new service reads the same scratch root from disk.
        fresh = ResearchService(drive=env.drive, scratch_root=env.scratch)
        resumed = fresh.resume_project(USER, pid)
        assert resumed["project_id"] == pid
        assert resumed["stage"] == "DISCOVER"
        lineage = fresh.query_lineage(USER, pid, node_id="S")
        assert lineage["node"]["label"] == "source"

    async def test_two_user_tenancy_isolation(self, env):
        pid_a = (await _create_project(env.runtime, name="A"))["project_id"]

        set_request_user(USER_B)
        pid_b = (await _create_project(env.runtime, name="B"))["project_id"]
        set_request_user(USER)

        assert (env.scratch / str(USER) / pid_a).is_dir()
        assert (env.scratch / str(USER_B) / pid_b).is_dir()
        assert not (env.scratch / str(USER) / pid_b).exists()

        # B cannot see A's project, and vice versa.
        with pytest.raises(ValueError):
            ResearchService(drive=env.drive, scratch_root=env.scratch).resume_project(USER, pid_b)
        with pytest.raises(ValueError):
            ResearchService(drive=env.drive, scratch_root=env.scratch).resume_project(USER_B, pid_a)


# ── 3. Three-layer storage: scratch -> cloud drive -> RAG projection ──────────
class TestThreeLayerStorage:
    async def test_promote_to_drive_triggers_rag_pending(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        written = await _run(
            env.runtime, "research_artifact", action="write_scratch", project_id=pid,
            artifact_id="report", content="# Report\n\nDraft body.",
        )
        assert written["status"] == "DRAFT"

        promoted = await _run(
            env.runtime, "research_artifact", action="promote_to_drive", project_id=pid,
            artifact_id="report",
        )
        assert promoted["status"] == "PROMOTED"
        assert promoted["drive_asset_id"]
        assert promoted["drive_path"] == f"research/{pid}/report.md"
        assert promoted["rag_status"] == "PENDING"

        # The drive now holds exactly one asset under research/<project_id>, RAG pending.
        assets = list(env.drive.assets.rows.values())
        assert len(assets) == 1
        assert assets[0].folder_path == f"research/{pid}"
        assert assets[0].rag_status == "PENDING"

        # Promotion also mirrors the report into the task folder's outputs/ projection.
        outputs = list((env.scratch / str(USER) / pid / "outputs").glob("*.md"))
        assert len(outputs) == 1
        assert outputs[0].name == "report.md"
        assert outputs[0].read_text(encoding="utf-8") == "# Report\n\nDraft body."

        # Re-promote is idempotent: same asset, no second drive write.
        again = await _run(
            env.runtime, "research_artifact", action="promote_to_drive", project_id=pid,
            artifact_id="report",
        )
        assert again["idempotent"] is True
        assert again["drive_asset_id"] == promoted["drive_asset_id"]
        assert len(list(env.drive.assets.rows.values())) == 1


# ── 4. Graph lineage + STALE/INVALID cascade ──────────────────────────────────
class TestGraphLineage:
    @staticmethod
    async def _build_chain(runtime, pid):
        # Canonical epistemic chain: Dataset -> Execution -> Result -> Evidence -> Claim.
        for nid, ntype in [("D", "Dataset"), ("EX", "Execution"), ("R", "Result"),
                           ("E", "Evidence"), ("C", "Claim")]:
            await _run(runtime, "research_evidence", action="record_node", project_id=pid,
                       node={"id": nid, "type": ntype, "label": nid})
        for src, dst, kind in [
            ("EX", "D", "derived_from"),   # Execution is derived from the Dataset
            ("R", "EX", "derived_from"),   # Result is derived from the Execution
            ("R", "E", "supports"),        # Result supports the Evidence
            ("E", "C", "supports"),        # Evidence supports the Claim
        ]:
            await _run(runtime, "research_evidence", action="link_edge", project_id=pid,
                       src=src, dst=dst, kind=kind)

    async def test_mutating_upstream_stale_cascades_downstream(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        await self._build_chain(env.runtime, pid)

        mutated = await _run(env.runtime, "research_evidence", action="mutate_node",
                             project_id=pid, node_id="D",
                             patch={"verification_status": "updated"})
        assert sorted(mutated["cascade"]) == ["C", "E", "EX", "R"]
        for node in mutated["cascade"]:
            assert node != "D"

        statuses = {}
        for nid in ("D", "EX", "R", "E", "C"):
            lineage = await _run(env.runtime, "research_evidence", action="query_lineage",
                                 project_id=pid, node_id=nid)
            statuses[nid] = lineage["node"]["status"]
        assert statuses == {"D": "VALID", "EX": "STALE", "R": "STALE", "E": "STALE", "C": "STALE"}

    async def test_invalidate_downstream_marks_invalid(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        await self._build_chain(env.runtime, pid)

        invalidated = await _run(env.runtime, "research_evidence", action="invalidate_downstream",
                                 project_id=pid, node_id="D")
        assert sorted(invalidated["cascade"]) == ["C", "E", "EX", "R"]

        d = await _run(env.runtime, "research_evidence", action="query_lineage",
                       project_id=pid, node_id="D")
        assert d["node"]["status"] == "INVALID"
        # Method refuted / evidence overturned: downstream is INVALID, not just STALE.
        for nid in ("EX", "R", "E", "C"):
            node = await _run(env.runtime, "research_evidence", action="query_lineage",
                              project_id=pid, node_id=nid)
            assert node["node"]["status"] == "INVALID"

    async def test_lineage_ancestors_and_descendants(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        await self._build_chain(env.runtime, pid)

        lineage = await _run(env.runtime, "research_evidence", action="query_lineage",
                             project_id=pid, node_id="C")
        assert lineage["ancestors"] == ["D", "E", "EX", "R"]
        assert lineage["descendants"] == []

        lineage = await _run(env.runtime, "research_evidence", action="query_lineage",
                             project_id=pid, node_id="D")
        assert lineage["ancestors"] == []
        assert lineage["descendants"] == ["C", "E", "EX", "R"]


# ── 5. Gate override flow ─────────────────────────────────────────────────────
class TestGateOverride:
    async def test_evidence_gate_passes_on_core_checks_only(self, env):
        # Literature MVP: verified source + evidence + draft claim -> PASS (no empirical run).
        pid = (await _create_project(env.runtime, profile="literature"))["project_id"]
        await _run(env.runtime, "research_evidence", action="record_node", project_id=pid,
                   node={"id": "S", "type": "Source", "label": "s", "verification_status": "verified"})
        await _run(env.runtime, "research_evidence", action="record_node", project_id=pid,
                   node={"id": "EV", "type": "Evidence", "label": "ev"})
        await _run(env.runtime, "research_evidence", action="record_node", project_id=pid,
                   node={"id": "CL", "type": "Claim", "label": "cl"})
        await _run(env.runtime, "research_evidence", action="link_edge", project_id=pid,
                   src="EV", dst="S", kind="depends_on")
        await _run(env.runtime, "research_evidence", action="link_edge", project_id=pid,
                   src="CL", dst="EV", kind="supports")

        result = await _run(env.runtime, "research_gate", action="check",
                            project_id=pid, gate_name="EVIDENCE_GATE")
        assert result["status"] == "PASS"
        assert all(c["ok"] for c in result["checks"])

    async def test_fail_on_unverified_source_then_human_override(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        await _walk_to(env.runtime, pid, "EXECUTE")

        # An unverified source makes EVIDENCE_GATE FAIL deterministically.
        await _run(env.runtime, "research_evidence", action="record_node", project_id=pid,
                   node={"id": "S", "type": "Source", "label": "s", "verification_status": "unverified"})
        failed = await _run(env.runtime, "research_gate", action="check",
                            project_id=pid, gate_name="EVIDENCE_GATE")
        assert failed["status"] == "FAIL"
        assert any(not c["ok"] and c["name"] == "sources_verified" for c in failed["checks"])

        # The guarded transition is rejected while the gate is un-passed.
        blocked = await _run(env.runtime, "research_state", action="transition_stage",
                             project_id=pid, target="EXPLAIN")
        assert blocked["granted"] is False
        assert "EVIDENCE_GATE" in blocked["reason"]

        # Override request spawns a PENDING approval with the PENDING-null invariant.
        pending = await _run(env.runtime, "research_gate", action="request_override",
                             project_id=pid, gate_name="EVIDENCE_GATE",
                             reason="literature profile has no empirical run")
        assert pending["status"] == "PENDING"
        assert pending["approver_user_id"] is None
        assert pending["resolved_at"] is None

        # A human approves -> the gate becomes OVERRIDE and the transition is granted.
        resolved = await _run(env.runtime, "research_gate", action="resolve_override",
                              approval_id=pending["approval_id"], approve=True)
        assert resolved["status"] == "APPROVED"
        assert resolved["approver_user_id"] == str(USER)
        assert resolved["resolved_at"] is not None

        override = await _run(env.runtime, "research_gate", action="check",
                              project_id=pid, gate_name="EVIDENCE_GATE")
        assert override["status"] == "OVERRIDE"

        granted = await _run(env.runtime, "research_state", action="transition_stage",
                             project_id=pid, target="EXPLAIN")
        assert granted["granted"] is True

    async def test_double_resolve_rejected(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        pending = await _run(env.runtime, "research_gate", action="request_override",
                             project_id=pid, gate_name="EVIDENCE_GATE", reason="x")
        await _run(env.runtime, "research_gate", action="resolve_override",
                   approval_id=pending["approval_id"], approve=True)

        result = await env.runtime.execute(
            ToolExecution(call_id=str(uuid.uuid4()), name="research_gate",
                          arguments={"action": "resolve_override",
                                     "approval_id": pending["approval_id"], "approve": True})
        )
        assert result.is_error is True
        assert "already resolved" in result.error.message


# ── 6. Idempotency + immutable executions ─────────────────────────────────────
class TestIdempotencyAndExecution:
    async def test_same_idempotency_key_returns_same_project(self, env):
        first = await _create_project(env.runtime, idempotency_key="proj-k1")
        second = await _create_project(env.runtime, idempotency_key="proj-k1")
        assert first["project_id"] == second["project_id"]
        assert second["idempotent"] is True
        projects = list((env.scratch / str(USER)).iterdir())
        assert len(projects) == 1

    async def test_same_idempotency_key_returns_same_artifact_version(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        first = await _run(env.runtime, "research_artifact", action="write_scratch",
                           project_id=pid, artifact_id="draft", content="v1",
                           idempotency_key="art-k1")
        second = await _run(env.runtime, "research_artifact", action="write_scratch",
                            project_id=pid, artifact_id="draft", content="v1-again",
                            idempotency_key="art-k1")
        assert first["artifact_id"] == second["artifact_id"]
        assert first["version"] == second["version"] == 1
        assert second["idempotent"] is True
        versions = list((env.scratch / str(USER) / pid / "artifacts" / "draft").glob("v*"))
        assert len(versions) == 1

    async def test_artifact_producer_invariant(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        execution = await _run(env.runtime, "research_run", action="record_execution",
                               project_id=pid, tool="research_evidence.record_node",
                               args={"node": "S"})
        execution_id = execution["execution_id"]

        # Agent-generated artifact: generated_by_execution non-null.
        await _run(env.runtime, "research_artifact", action="write_scratch", project_id=pid,
                   artifact_id="agent_out", content="x", generated_by_execution=execution_id)
        agent_record = ResearchService._load_json(
            env.scratch / str(USER) / pid / "artifacts" / "agent_out" / "v1", None
        )
        assert agent_record["generated_by_execution"] == execution_id

        # User intake: created_by non-null, generated_by_execution null.
        await _run(env.runtime, "research_artifact", action="write_scratch", project_id=pid,
                   artifact_id="user_note", content="y")
        user_record = ResearchService._load_json(
            env.scratch / str(USER) / pid / "artifacts" / "user_note" / "v1", None
        )
        assert user_record["created_by"] == str(USER)
        assert user_record["generated_by_execution"] is None

    async def test_execution_is_immutable_after_success(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        execution = await _run(env.runtime, "research_run", action="record_execution",
                               project_id=pid, tool="research_evidence.link_edge", args={})
        execution_id = execution["execution_id"]
        assert execution["status"] == "RUNNING"

        done = await _run(env.runtime, "research_run", action="finish_execution",
                          project_id=pid, execution_id=execution_id, result={"edges": 1})
        assert done["status"] == "SUCCESS"

        result = await env.runtime.execute(
            ToolExecution(call_id=str(uuid.uuid4()), name="research_run",
                          arguments={"action": "finish_execution", "project_id": pid,
                                     "execution_id": execution_id, "result": {}})
        )
        assert result.is_error is True
        assert "immutable" in result.error.message


# ── 7. Chat-driven tasks: atomic create, materials tenancy, session binding ──
class TestChatTasks:
    async def test_create_task_atomic_folder_layout(self, env):
        asset = await env.drive.save_artifact(
            USER, name="paper.pdf", mime_type="application/pdf", content=b"%PDF"
        )
        svc = ResearchService(env.drive, env.scratch)
        created = await svc.create_task(
            USER,
            title="VecDB compare",
            description="recall vs latency",
            material_asset_ids=[str(asset.id)],
        )
        task_id = created["task_id"]
        assert created["stage"] == "DISCOVER"
        assert created["status"] == "ACTIVE"
        assert created["idempotent"] is False
        assert created["cloud_folder_path"] == "VecDB compare"
        assert created["materials"][0]["asset_id"] == str(asset.id)

        # Scratch state is authoritative and complete.
        task_dir = env.scratch / str(USER) / task_id
        for f in ("project.json", "graph.json", "task_spec.json", "session_history.json"):
            assert (task_dir / f).is_file(), f
        project = ResearchService._load_json(task_dir / "project.json", None)
        assert project["cloud_folder_id"]
        assert project["cloud_folder_path"] == "VecDB compare"
        assert project["materials"][0]["cloud_asset_id"]
        assert project["materials"][0]["mime"] == "application/pdf"
        assert project["materials"][0]["name"] == "paper.pdf"
        spec = ResearchService._load_json(task_dir / "task_spec.json", None)
        assert spec["title"] == "VecDB compare"
        assert spec["description"] == "recall vs latency"
        assert spec["created_by"] == str(USER)
        mirror = ResearchService._load_json(task_dir / "session_history.json", None)
        assert mirror == {"session_id": None, "turns": []}

        # Cloud projection: the task folder + material asset (name <asset_id>__<safe_name>).
        folders = [f["name"] for f in await env.drive.list_folders(USER)]
        assert "VecDB compare" in folders
        files = await env.drive.list_files(USER)
        mats = [a for a in files if a["folder_path"] == "VecDB compare/materials"]
        assert len(mats) == 1
        assert mats[0]["name"] == f"{asset.id}__paper.pdf"
        # get_task_status lists materials from the cloud projection.
        status = await svc.get_task_status(USER, task_id)
        assert status["materials"] == [f"{asset.id}__paper.pdf"]

        listed = svc.list_tasks(USER)
        assert [t["task_id"] for t in listed] == [task_id]
        assert listed[0]["stage"] == "DISCOVER"

    async def test_create_task_with_parent_folder(self, env):
        svc = ResearchService(env.drive, env.scratch)
        created = await svc.create_task(USER, title="nested", parent_folder_path="Projects")
        project = ResearchService._load_json(
            env.scratch / str(USER) / created["task_id"] / "project.json", None
        )
        # The working directory is honored: the task folder lands under Projects/.
        assert project["cloud_folder_path"] == "Projects/nested"
        folders = [f["path"] for f in await env.drive.list_folders(USER)]
        assert "Projects/nested" in folders

    async def test_create_task_rolls_back_on_material_failure(self, env):
        svc = ResearchService(env.drive, env.scratch)
        with pytest.raises(DriveError) as exc:
            await svc.create_task(USER, title="t", material_asset_ids=[str(uuid.uuid4())])
        assert exc.value.status_code == 404
        assert svc.list_tasks(USER) == []  # atomic: no half-built task folder left behind
        assert await env.drive.list_folders(USER) == []  # cloud folder rolled back too

    async def test_create_task_cross_user_material_denied(self, env):
        asset = await env.drive.save_artifact(
            USER, name="secret.txt", mime_type="text/plain", content=b"top secret"
        )
        svc = ResearchService(env.drive, env.scratch)
        with pytest.raises(DriveError) as exc:
            await svc.create_task(USER_B, title="sneak", material_asset_ids=[str(asset.id)])
        assert exc.value.status_code == 403
        assert svc.list_tasks(USER_B) == []
        assert await env.drive.list_folders(USER_B) == []  # no leftover cloud task folder

    async def test_create_task_idempotent(self, env):
        svc = ResearchService(env.drive, env.scratch)
        first = await svc.create_task(USER, title="t", idempotency_key="task-k1")
        second = await svc.create_task(USER, title="t2", idempotency_key="task-k1")
        assert first["task_id"] == second["task_id"]
        assert second["idempotent"] is True

    async def test_list_tasks_dedups_stray_scratch_dirs(self, env):
        # A crash mid-create could leave a stray scratch dir claiming a task_id that another
        # dir owns. list_tasks must return each task exactly once so the monitor never renders
        # a task twice (the frontend clears its container, but the list source must be unique).
        svc = ResearchService(env.drive, env.scratch)
        created = await svc.create_task(USER, title="dup")
        task_id = created["task_id"]
        task_dir = env.scratch / str(USER) / task_id
        stray = env.scratch / str(USER) / f"{task_id}-stray"
        stray.mkdir(parents=True)
        (stray / "project.json").write_text((task_dir / "project.json").read_text(), encoding="utf-8")
        listed = svc.list_tasks(USER)
        assert [t["task_id"] for t in listed] == [task_id]

    async def test_bind_session_one_session_one_task(self, env):
        svc = ResearchService(env.drive, env.scratch)
        a = (await svc.create_task(USER, title="A"))["task_id"]
        b = (await svc.create_task(USER, title="B"))["task_id"]
        session = uuid.uuid4()
        assert svc.bind_session(USER, a, session)["task_id"] == a
        assert svc.bind_session(USER, a, session)["task_id"] == a  # same pair is idempotent
        with pytest.raises(ValueError) as exc:
            svc.bind_session(USER, b, session)
        assert "already bound" in str(exc.value)
        assert svc.task_id_for_session(USER, session) == a
        assert svc.bound_session_ids(USER) == {str(session)}  # chat sidebar filter set
        mirror = ResearchService._load_json(
            env.scratch / str(USER) / a / "session_history.json", None
        )
        assert mirror["session_id"] == str(session)

    async def test_append_session_turn_mirrors_into_bound_task(self, env):
        svc = ResearchService(env.drive, env.scratch)
        a = (await svc.create_task(USER, title="A"))["task_id"]
        session = uuid.uuid4()
        svc.bind_session(USER, a, session)
        await svc.append_session_turn(USER, session, "user", "hello")
        await svc.append_session_turn(USER, session, "assistant", "world")
        mirror = ResearchService._load_json(
            env.scratch / str(USER) / a / "session_history.json", None
        )
        assert [t["role"] for t in mirror["turns"]] == ["user", "assistant"]
        assert mirror["turns"][0]["content"] == "hello"
        # get_task_status surfaces the bound session transcript for the monitor.
        status = await svc.get_task_status(USER, a)
        assert status["session"]["session_id"] == str(session)
        assert [t["content"] for t in status["session"]["turns"]] == ["hello", "world"]
        # The turn is also mirrored into the cloud folder's session_history.json, in place.
        files = await env.drive.list_files(USER)
        mirror_assets = [a for a in files if a["name"] == "session_history.json"]
        assert len(mirror_assets) == 1  # updated in place, not re-created per turn

    async def test_get_task_status_groups_nodes_and_counts(self, env):
        svc = ResearchService(env.drive, env.scratch)
        a = (await svc.create_task(USER, title="A", description="desc"))["task_id"]
        svc.record_node(USER, a, node={"id": "S", "type": "Source", "label": "s"})
        svc.record_node(USER, a, node={"id": "C", "type": "Claim", "label": "c"})
        status = await svc.get_task_status(USER, a)
        assert status["description"] == "desc"
        assert status["stage"] == "DISCOVER"
        assert sorted(status["nodes"]) == ["Claim", "Source"]
        assert status["materials"] == []
        assert status["outputs"] == []
        # The two work folders exist even with nothing in them, so the working-directory
        # layout is visible from the start.
        folders = [f["path"] for f in await env.drive.list_folders(USER)]
        assert f"{status['cloud_folder_path']}/materials" in folders
        assert f"{status['cloud_folder_path']}/outputs" in folders
        # Only the two root mirrors exist — nothing in materials/ or outputs/ yet.
        assert {f["name"] for f in status["cloud_files"]} == {"task_spec.json", "session_history.json"}
        assert {f["folder_path"] for f in status["cloud_files"]} == {status["cloud_folder_path"]}
        assert status["task_id"] == a


# ── 8. Cascade delete: 409 guards + soft cloud delete + hard scratch delete ──
class TestDeleteTask:
    async def test_delete_running_task_is_blocked(self, env):
        svc = ResearchService(env.drive, env.scratch)
        task_id = (await svc.create_task(USER, title="busy"))["task_id"]
        svc.record_execution(USER, task_id, tool="research_run.execute_sandbox_script", args={})
        with pytest.raises(ValueError) as exc:
            await svc.delete_task(USER, task_id)
        assert "currently running" in str(exc.value)
        assert svc.list_tasks(USER)  # the 409 guard leaves the task in place

    async def test_delete_indexed_report_is_blocked(self, env):
        svc = ResearchService(env.drive, env.scratch)
        task_id = (await svc.create_task(USER, title="kb"))["task_id"]
        await svc.write_scratch(USER, task_id, artifact_id="report", content="# R")
        await svc.promote_to_drive(USER, task_id, artifact_id="report")
        # The RAG worker indexed the outputs asset → deletion is blocked.
        report = next(a for a in env.drive.assets.rows.values() if a.name == "report.md")
        await env.drive.assets.set_status(report.id, rag_status="INDEXED")
        with pytest.raises(ValueError) as exc:
            await svc.delete_task(USER, task_id)
        assert "Knowledge Base" in str(exc.value)
        assert svc.list_tasks(USER)

    async def test_delete_marks_request_then_cascades(self, env):
        svc = ResearchService(env.drive, env.scratch)
        task_id = (await svc.create_task(USER, title="doomed"))["task_id"]
        project = ResearchService._load_json(
            env.scratch / str(USER) / task_id / "project.json", None
        )
        cloud_id = project["cloud_folder_id"]

        # Simulate a crash between "mark" and "teardown": a non-DriveError cloud failure
        # propagates (delete_task swallows DriveError only), leaving the marked project.json.
        original_delete = env.drive.delete_folder

        async def boom(*args, **kwargs):
            raise RuntimeError("cloud unavailable")

        env.drive.delete_folder = boom
        with pytest.raises(RuntimeError):
            await svc.delete_task(USER, task_id)
        env.drive.delete_folder = original_delete

        marked = ResearchService._load_json(
            env.scratch / str(USER) / task_id / "project.json", None
        )
        assert marked["deletion_requested"] is True

        # Happy path: cloud folder soft-deleted, scratch state hard-deleted.
        await svc.delete_task(USER, task_id)
        assert not (env.scratch / str(USER) / task_id).exists()
        assert svc.list_tasks(USER) == []
        assert [f for f in env.drive.folders.rows.values() if f.id == uuid.UUID(cloud_id)] == []
        assert [a for a in env.drive.assets.rows.values() if a.deleted_at is None] == []

    async def test_delete_traversal_is_rejected(self, env):
        svc = ResearchService(env.drive, env.scratch)
        with pytest.raises(ValueError):
            await svc.delete_task(USER, "../evil")

    async def test_delete_active_run_is_blocked(self, env):
        svc = ResearchService(env.drive, env.scratch)
        task_id = (await svc.create_task(USER, title="busy"))["task_id"]
        # A live server-owned run (not just a per-tool execution) blocks deletion too.
        run = svc.begin_run(USER, task_id, session_id="sess-1")
        assert run["status"] == "RUNNING"
        with pytest.raises(ValueError) as exc:
            await svc.delete_task(USER, task_id)
        assert "currently running" in str(exc.value)
        assert svc.list_tasks(USER)  # the 409 guard leaves the task in place


# ── 9. Single-task run mutex (begin_run/end_run + stale-window crash recovery) ──
class TestRunMutex:
    async def test_begin_conflict_then_end_releases(self, env):
        svc = ResearchService(env.drive, env.scratch)
        task_id = (await svc.create_task(USER, title="A"))["task_id"]
        assert svc.list_tasks(USER)[0]["is_running"] is False

        run = svc.begin_run(USER, task_id, session_id="sess-1")
        assert run["status"] == "RUNNING"
        assert run["session_id"] == "sess-1"
        assert svc.list_tasks(USER)[0]["is_running"] is True

        # A second concurrent trigger for the SAME task is a conflict.
        with pytest.raises(ValueError) as exc:
            svc.begin_run(USER, task_id, session_id="sess-2")
        assert "already running" in str(exc.value)
        assert (await svc.get_task_status(USER, task_id))["is_running"] is True

        # Release, then re-acquire works — the mutex is per-run, not permanent.
        released = svc.end_run(USER, task_id)
        assert released["status"] == "IDLE"
        assert svc.list_tasks(USER)[0]["is_running"] is False
        again = svc.begin_run(USER, task_id, session_id="sess-3")
        assert again["run_id"] != run["run_id"]
        svc.end_run(USER, task_id)

    async def test_two_tasks_run_concurrently(self, env):
        svc = ResearchService(env.drive, env.scratch)
        a = (await svc.create_task(USER, title="A"))["task_id"]
        b = (await svc.create_task(USER, title="B"))["task_id"]
        # T4 invariant #2: Task A and Task B may both hold a live run at once.
        ra = svc.begin_run(USER, a)
        rb = svc.begin_run(USER, b)
        assert ra["run_id"] != rb["run_id"]
        assert {t["task_id"]: t["is_running"] for t in svc.list_tasks(USER)} == {a: True, b: True}
        svc.end_run(USER, a)
        svc.end_run(USER, b)

    async def test_stale_run_adopted_and_stale_executions_aborted(self, env):
        svc = ResearchService(env.drive, env.scratch)
        task_id = (await svc.create_task(USER, title="crashed"))["task_id"]
        svc.record_execution(USER, task_id, tool="research_run.execute_sandbox_script", args={})
        # Simulate a process that died mid-run: the slot is RUNNING and old, with an execution
        # still marked RUNNING (the kill happened before finish_execution).
        project_path = env.scratch / str(USER) / task_id / "project.json"
        project = ResearchService._load_json(project_path, None)
        project["active_run"] = {
            "run_id": "dead-run",
            "session_id": "sess-dead",
            # 3 hours ago — comfortably past the default 2h stale window.
            "started_at": "1970-01-01T00:00:00Z",
            "status": "RUNNING",
        }
        ResearchService._save_json(project_path, project)

        adopted = svc.begin_run(USER, task_id, session_id="sess-fresh")
        assert adopted["status"] == "RUNNING"
        assert adopted["run_id"] != "dead-run"
        # The dead process's RUNNING execution is ABORTED so the delete guard can't block forever.
        executions = ResearchService._load_json(
            env.scratch / str(USER) / task_id / "executions.json", {"executions": []}
        )["executions"]
        assert all(e["status"] == "ABORTED" for e in executions)
        svc.end_run(USER, task_id)
