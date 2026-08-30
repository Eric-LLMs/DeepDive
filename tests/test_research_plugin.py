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
    current = "INBOX"
    order = ["DISCOVER", "FRAME", "EVIDENCE", "DESIGN", "EXECUTE", "EXPLAIN", "WRITE"]
    for target in order:
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
        assert project["stage"] == "INBOX"
        assert project["status"] == "ACTIVE"

    async def test_crash_resume_via_fresh_service(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        await _run(env.runtime, "research_evidence", action="record_node", project_id=pid,
                   node={"id": "S", "type": "Source", "label": "source"})

        # Simulate a crash: a brand-new service reads the same scratch root from disk.
        fresh = ResearchService(drive=env.drive, scratch_root=env.scratch)
        resumed = fresh.resume_project(USER, pid)
        assert resumed["project_id"] == pid
        assert resumed["stage"] == "INBOX"
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
