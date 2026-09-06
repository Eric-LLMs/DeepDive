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

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from agent import Context, PluginManager, SkillRegistry, ToolRuntime
from agent.engine.context import AgentTurn, bind_turn
from agent.engine.decisions import ToolExecution
from core.application.drive_service import DriveError
from core.infrastructure.request_context import set_request_user
from core.infrastructure.web_fetch import MIN_FETCH_TEXT_CHARS

from plugins.research.plugin import (
    _GATE_NOTE_KEY,
    _GATES,
    _REASON_LIMIT,
    ResearchService,
    compose_gate_review_note,
    register_research_plugins,
)
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
    "research_scrape",
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

    async def test_all_research_tools_registered(self, env):
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


# ── 6.5 Gate review notes (deterministic chat explanation) ───────────────────
class TestGateReviewNotes:
    # The auto ``system`` note a parked gate writes into the task chat: composed purely from
    # the mechanical check results + a trimmed agent reason, persisted to the session DB
    # BEFORE its approval id is CAS-marked (never marker-first), and never via check_gate
    # (which would record a verdict). These tests prove the compose/draft/mark/emit contract.
    async def _failing_evidence(self, env):
        """A fresh project with an unverified Source + a PENDING EVIDENCE_GATE override."""
        pid = (await _create_project(env.runtime, profile="literature"))["project_id"]
        await _run(env.runtime, "research_evidence", action="record_node", project_id=pid,
                   node={"id": "S", "type": "Source", "label": "s",
                         "verification_status": "unverified"})
        pending = await _run(env.runtime, "research_gate", action="request_override",
                             project_id=pid, gate_name="EVIDENCE_GATE",
                             reason="literature profile has no empirical run")
        return ResearchService(env.drive, env.scratch), pid, pending["approval_id"]

    def test_compose_includes_gate_label_failed_checks_and_risk(self):
        note = compose_gate_review_note("EVIDENCE_GATE", [
            {"name": "sources_verified", "ok": False,
             "detail": "need >=1 Source with verification_status='verified'"},
            {"name": "claim_draft_links", "ok": False,
             "detail": ">=1 draft Claim linked to an Evidence node"},
        ], reason="no verified corpus yet")
        assert "Evidence Gate did not pass." in note
        assert "• sources_verified: every source used as fact must exist" in note
        assert "Gate detail: need >=1 Source" in note
        assert "• claim_draft_links:" in note
        assert "Why it matters:" in note
        assert "Agent's request: no verified corpus yet" in note

    def test_compose_skips_ok_checks_and_empty_reason(self):
        # A green check must never appear as a failure bullet.
        note = compose_gate_review_note("EVIDENCE_GATE", [
            {"name": "sources_verified", "ok": True, "detail": "fine"},
            {"name": "no_invalid_upstream", "ok": False, "detail": "broken"},
        ], reason="   ")
        assert "sources_verified" not in note  # the ok check is not listed
        assert "no_invalid_upstream" in note
        assert "Agent's request" not in note

    def test_compose_reason_trimmed_to_limit(self):
        note = compose_gate_review_note(
            "DESIGN_GATE",
            [{"name": "design_fields", "ok": False, "detail": "missing fields"}],
            reason="r" * (_REASON_LIMIT + 500),
        )
        agent_line = next(ln for ln in note.splitlines() if ln.startswith("Agent's request:"))
        assert agent_line.endswith("…")
        assert len(agent_line) <= len("Agent's request: ") + _REASON_LIMIT + 1

    def test_compose_green_checks_still_emits_decision_note(self):
        note = compose_gate_review_note("QUALITY_GATE", [])
        assert "(the gate's checks are green here" in note
        assert "Approve to continue" in note

    async def test_drafts_are_readonly_and_list_failed_checks(self, env):
        svc, pid, approval_id = await self._failing_evidence(env)
        task_dir = env.scratch / str(USER) / pid
        proj_before = (task_dir / "project.json").read_text()
        appr_before = (task_dir / "approvals.json").read_text()

        drafts = svc.gate_note_drafts(USER, pid)

        assert len(drafts) == 1
        assert drafts[0]["approval_id"] == approval_id
        assert "sources_verified" in drafts[0]["text"]  # the unverified source's red check
        assert "Agent's request: literature profile has no empirical run" in drafts[0]["text"]
        # Read-only: no gate verdict, no approvals change, no marker write.
        assert (task_dir / "project.json").read_text() == proj_before
        assert (task_dir / "approvals.json").read_text() == appr_before

    async def test_mark_is_idempotent_and_notes_are_consumed(self, env):
        svc, pid, approval_id = await self._failing_evidence(env)
        task_dir = env.scratch / str(USER) / pid

        svc.mark_gate_notes(USER, pid, [approval_id])
        project = ResearchService._load_json(task_dir / "project.json", None)
        assert project[_GATE_NOTE_KEY] == [approval_id]

        # Re-marking the same id is a no-op (still one entry), and drafts go quiet.
        svc.mark_gate_notes(USER, pid, [approval_id])
        assert ResearchService._load_json(task_dir / "project.json", None)[_GATE_NOTE_KEY] == [approval_id]
        assert svc.gate_note_drafts(USER, pid) == []

    async def test_mark_skips_resolved_and_unknown_ids(self, env):
        svc, pid, approval_id = await self._failing_evidence(env)
        task_dir = env.scratch / str(USER) / pid
        svc.resolve_override(USER, approval_id, approve=False)  # REJECTED -> no longer PENDING
        svc.mark_gate_notes(USER, pid, [approval_id, "does-not-exist"])
        project = ResearchService._load_json(task_dir / "project.json", None)
        assert project.get(_GATE_NOTE_KEY, []) == []

    async def test_emit_db_failure_never_consumes_marker(self, env, monkeypatch):
        svc, pid, _approval_id = await self._failing_evidence(env)
        marked = []

        async def boom(*_args, **_kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr("core.infrastructure.memory.insert_plain_message", boom)
        monkeypatch.setattr(svc, "mark_gate_notes",
                            lambda *a, **k: marked.append((a, k)))

        written = await svc.emit_gate_notes(None, USER, pid, str(uuid.uuid4()))

        assert written == 0
        assert marked == []  # DB never committed -> marker untouched -> a retry can succeed later
        project = ResearchService._load_json(env.scratch / str(USER) / pid / "project.json", None)
        assert project.get(_GATE_NOTE_KEY, []) == []

    async def test_emit_writes_db_then_marks(self, env, monkeypatch):
        """DB insert (role system) commits before the id is CAS-marked; written == 1."""
        svc, pid, approval_id = await self._failing_evidence(env)
        added = []
        marked = []

        class _Session:
            def __init__(self, log):
                self._log = log

            def add(self, obj):
                self._log.append(obj)

            async def commit(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        def factory():
            return _Session(added)

        monkeypatch.setattr(svc, "mark_gate_notes",
                            lambda *a, **k: marked.append((a, k)))

        written = await svc.emit_gate_notes(factory, USER, pid, str(uuid.uuid4()))

        assert written == 1
        assert len(added) == 1
        assert added[0].role == "system"
        assert "sources_verified" in added[0].text
        assert marked == [( (USER, pid, [approval_id]), {} )]


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
    async def test_delete_orphan_running_execution_succeeds(self, env):
        # A run that stopped mid-step releases its slot (end_run) but can leave the interrupted
        # tool call marked RUNNING in executions.json. With no live slot that row is an orphan —
        # record_execution ran but finish_execution never did — and must not block deletion
        # forever. Regression: delete used to guard on ANY RUNNING execution row, which made a
        # cancelled/crashed task permanently undeletable even though nothing was running.
        svc = ResearchService(env.drive, env.scratch)
        task_id = (await svc.create_task(USER, title="orphan"))["task_id"]
        svc.begin_run(USER, task_id, session_id="sess-1")
        execution = svc.record_execution(
            USER, task_id, tool="research_run.execute_sandbox_script", args={}
        )
        assert execution["status"] == "RUNNING"
        svc.end_run(USER, task_id)  # the driver stopped; the interrupted execution was never finished
        assert svc.list_tasks(USER)[0]["is_running"] is False

        await svc.delete_task(USER, task_id)
        assert not (env.scratch / str(USER) / task_id).exists()
        assert svc.list_tasks(USER) == []

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


# ── 7. Worker-turn arg resolution (handoff-bound, no raw KeyError) ───────────
class TestWorkerArgResolution:
    # The worker auto-run drives research tools WITHOUT repeating project_id in every call: the
    # task id is threaded via current_turn().context["handoff"], so an omitted project_id must
    # resolve to the bound project (not a KeyError), and a missing action argument must surface
    # a precise, actionable message the model can correct instead of a bare KeyError repr.

    async def _bound(self, pid: str):
        """Bind a worker-style turn context carrying the research handoff for ``pid``."""
        bind_turn(
            AgentTurn(
                user_msg="probe",
                context={"handoff": {"kind": "research", "project_id": pid, "mode": "research_resume"}},
            )
        )

    async def test_omitted_project_id_resolves_from_bound_handoff(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        await self._bound(pid)
        try:
            # Every research tool that targets a project must recover the id from the handoff.
            await _run(env.runtime, "research_state", action="get_state")
            await _run(env.runtime, "research_evidence", action="record_node",
                       node={"id": "S", "type": "Source", "label": "s",
                             "verification_status": "verified"})
            await _run(env.runtime, "research_evidence", action="link_edge",
                       src="S", dst="S", kind="supports")
            await _run(env.runtime, "research_artifact", action="write_scratch",
                       artifact_id="m.md", content="# m")
            # The writes actually landed on the bound project.
            state = await _run(env.runtime, "research_state", action="get_state")
            assert state["stage"] == "DISCOVER"
            node = await _run(env.runtime, "research_evidence", action="query_lineage",
                              node_id="S")
            assert node["node"]["label"] == "s"
        finally:
            bind_turn(None)

    async def test_missing_action_arg_reports_precise_error(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        await self._bound(pid)
        try:
            for action_args, fragment in (
                ({"action": "link_edge", "src": "S", "dst": "T"},
                 "research_evidence link_edge is missing required argument 'kind'"),
                ({"action": "query_lineage"},
                 "research_evidence query_lineage is missing required argument 'node_id'"),
                ({"action": "mutate_node", "node_id": "S"},
                 "research_evidence mutate_node is missing required argument 'patch'"),
            ):
                result = await env.runtime.execute(
                    ToolExecution(call_id=str(uuid.uuid4()), name="research_evidence",
                                  arguments=action_args)
                )
                assert result.is_error is True
                assert fragment in result.error.message
                assert not result.error.message.startswith("'")  # never a bare KeyError repr
        finally:
            bind_turn(None)

    async def test_missing_project_id_outside_handoff_is_actionable(self, env):
        # No turn context bound: the id cannot be recovered, so the tool says so plainly.
        pid = (await _create_project(env.runtime))["project_id"]
        result = await env.runtime.execute(
            ToolExecution(call_id=str(uuid.uuid4()), name="research_state",
                          arguments={"action": "get_state"})
        )
        assert result.is_error is True
        assert "research_state get_state needs a project_id" in result.error.message
        assert str(pid) not in result.error.message  # never leaks a stale/wrong id

    async def test_record_node_accepts_stringified_node(self, env):
        # Some providers double-encode an object param into a JSON *string*; the tool must
        # decode it before the handler runs (the historical record_node freeze root cause).
        pid = (await _create_project(env.runtime))["project_id"]
        await _run(env.runtime, "research_evidence", action="record_node", project_id=pid,
                   node=json.dumps({"id": "S", "type": "Source", "label": "s"}))
        node = await _run(env.runtime, "research_evidence", action="query_lineage",
                          project_id=pid, node_id="S")
        assert node["node"]["type"] == "Source"

    async def test_record_node_without_id_reports_precise_error(self, env):
        # A probe node like {type, title} (no id) must not surface a bare ``KeyError: 'id'`` —
        # the model cannot correct from that repr. The guard names the missing keys.
        pid = (await _create_project(env.runtime))["project_id"]
        result = await env.runtime.execute(
            ToolExecution(
                call_id=str(uuid.uuid4()), name="research_evidence",
                arguments={"action": "record_node", "project_id": pid,
                           "node": {"type": "Source", "title": "probe"}},
            )
        )
        assert result.is_error is True
        assert "'id' and 'type'" in result.error.message
        assert not result.error.message.startswith("'")  # never a bare KeyError repr
        result_str = await env.runtime.execute(
            ToolExecution(
                call_id=str(uuid.uuid4()), name="research_evidence",
                arguments={"action": "record_node", "project_id": pid,
                           "node": '{"type": "Source", "title": "probe"}'},
            )
        )
        assert result_str.is_error is True
        assert "'id' and 'type'" in result_str.error.message


# ── execution_mode: strict (regression) / progressive (record + advance) ─────
# Guarded transition edges (target -> (guarding gate, legal predecessor stage)). Every guarded
# target is entered at most once per run because ``_LEGAL_NEXT`` is a strictly forward chain —
# so a (gate, stage) diagnostic is structurally unique and needs no de-dup logic.
_GUARDED = {
    "EXECUTE": ("DESIGN_GATE", "DESIGN"),
    "EXPLAIN": ("EVIDENCE_GATE", "EXECUTE"),
    "REVIEW": ("CLAIM_GATE", "WRITE"),
    "REPRODUCE": ("QUALITY_GATE", "REVIEW"),
}


class TestExecutionMode:
    @staticmethod
    def _svc(env) -> ResearchService:
        return ResearchService(drive=env.drive, scratch_root=env.scratch)

    @staticmethod
    def _project(env, pid: str) -> dict:
        return ResearchService._load_json(env.scratch / str(USER) / pid / "project.json", None)

    @staticmethod
    def _fast_stage(env, pid: str, stage: str) -> None:
        """Directly set the on-disk stage (skips the guards under test)."""
        TestExecutionMode._svc(env).atomic_update_project(
            USER, pid, lambda p: p.update(stage=stage)
        )

    async def test_create_defaults_strict_and_persists_mode(self, env):
        strict_pid = (await _create_project(env.runtime))["project_id"]
        prog_pid = (await _create_project(env.runtime, execution_mode="progressive"))["project_id"]
        assert self._project(env, strict_pid)["execution_mode"] == "strict"
        assert self._project(env, prog_pid)["execution_mode"] == "progressive"
        assert self._svc(env).resume_project(USER, prog_pid)["execution_mode"] == "progressive"

        # A legacy project persisted before this feature (no key) must behave as strict.
        legacy = self._project(env, strict_pid)
        del legacy["execution_mode"]
        svc = self._svc(env)
        svc._save_json(svc._project_dir(USER, strict_pid) / "project.json", legacy)
        assert svc.resume_project(USER, strict_pid)["execution_mode"] == "strict"

    async def test_create_rejects_unknown_execution_mode(self, env):
        result = await env.runtime.execute(
            ToolExecution(
                call_id=str(uuid.uuid4()), name="research_project",
                arguments={"action": "create", "name": "x", "execution_mode": "turbo"},
            )
        )
        assert result.is_error is True
        assert "execution_mode" in result.error.message

    async def test_strict_blocks_every_unpassed_guard_without_diagnostics(self, env):
        # Regression: the default (and any project without a mode) still hard-blocks all four
        # guarded transitions when the guard is not PASS/OVERRIDE, and records nothing.
        for target, (_gate, before) in _GUARDED.items():
            pid = (await _create_project(env.runtime))["project_id"]  # strict default
            self._fast_stage(env, pid, before)
            res = await _run(env.runtime, "research_state", action="transition_stage",
                             project_id=pid, target=target)
            assert res["granted"] is False, target
            project = self._project(env, pid)
            assert project["stage"] == before
            assert "diagnostics" not in project

    async def test_progressive_records_and_advances_every_guard(self, env):
        # Progressive does NOT weaken the checks: each guard still runs its real deterministic
        # checks (all genuinely FAIL on an empty/immature project), records the failed checks,
        # and — only then — lets the stage advance. The gate's own state is never forged to
        # PASS (it stays exactly what it was before the transition).
        for target, (gate, before) in _GUARDED.items():
            pid = (await _create_project(env.runtime, execution_mode="progressive"))["project_id"]
            self._fast_stage(env, pid, before)
            gates_before = self._project(env, pid)["gates"].get(gate)
            res = await _run(env.runtime, "research_state", action="transition_stage",
                             project_id=pid, target=target)
            assert res["granted"] is True, f"{target}: {res}"
            assert res["gate"] == gate
            project = self._project(env, pid)
            assert project["stage"] == target
            diags = project["diagnostics"]
            assert len(diags) == 1, (target, diags)
            d = diags[0]
            assert d["gate"] == gate and d["stage"] == before and d["target"] == target
            assert isinstance(d["timestamp"], str) and d["failed_checks"]
            # Real gate check functions ran: rows keep the gate's own {name, ok, detail} shape.
            assert all({"name", "ok", "detail"} <= set(c) for c in d["failed_checks"])
            assert all(c["ok"] is False for c in d["failed_checks"])
            # Gate verdict is unchanged — diagnostics never forge a PASS.
            assert project["gates"].get(gate) == gates_before

    async def test_progressive_passed_gate_not_recorded_failed_one_is(self, env):
        # A gate that genuinely PASSES must not be recorded; a later FAILING one is.
        pid = (await _create_project(env.runtime, execution_mode="progressive"))["project_id"]
        await _walk_to(env.runtime, pid, "EXECUTE")  # DESIGN_GATE genuinely passes here
        # An unverified Source makes EVIDENCE_GATE FAIL deterministically.
        await _run(env.runtime, "research_evidence", action="record_node", project_id=pid,
                   node={"id": "S", "type": "Source", "label": "s",
                         "verification_status": "unverified"})
        failed = await _run(env.runtime, "research_gate", action="check",
                            project_id=pid, gate_name="EVIDENCE_GATE")
        assert failed["status"] == "FAIL"
        gates_before = self._project(env, pid)["gates"]["EVIDENCE_GATE"]
        res = await _run(env.runtime, "research_state", action="transition_stage",
                         project_id=pid, target="EXPLAIN")
        assert res["granted"] is True
        project = self._project(env, pid)
        recorded = [d["gate"] for d in project["diagnostics"]]
        assert "EVIDENCE_GATE" in recorded and "DESIGN_GATE" not in recorded
        assert project["gates"]["EVIDENCE_GATE"] == gates_before

    async def test_progressive_still_enforces_state_machine_hard_constraints(self, env):
        pid = (await _create_project(env.runtime, execution_mode="progressive"))["project_id"]
        unknown = await _run(env.runtime, "research_state", action="transition_stage",
                             project_id=pid, target="NOPE")
        assert unknown["granted"] is False and "unknown" in unknown["reason"]
        skip = await _run(env.runtime, "research_state", action="transition_stage",
                          project_id=pid, target="WRITE")  # DISCOVER -> WRITE is illegal
        assert skip["granted"] is False and "illegal" in skip["reason"]
        assert "diagnostics" not in self._project(env, pid)

    async def test_progressive_guarded_target_recorded_at_most_once(self, env):
        # After the diagnostic the stage has moved on; re-targeting the now-occupied stage is
        # the benign ALREADY_AT_TARGET no-op (granted, but nothing written), so the entry and
        # its diagnostic stay structurally unique.
        # (No revision arithmetic here: the tool's monitor wrapper adds an advisory
        # publish_change bump after any mutating action, so deltas are not a clean proxy.)
        pid = (await _create_project(env.runtime, execution_mode="progressive"))["project_id"]
        self._fast_stage(env, pid, "DESIGN")
        first = await _run(env.runtime, "research_state", action="transition_stage",
                           project_id=pid, target="EXECUTE")
        assert first["granted"] is True and first["transition"] == "ADVANCED"
        second = await _run(env.runtime, "research_state", action="transition_stage",
                            project_id=pid, target="EXECUTE")
        assert second["granted"] is True and second["transition"] == "ALREADY_AT_TARGET"
        project = self._project(env, pid)
        assert project["stage"] == "EXECUTE"
        diags = project["diagnostics"]
        # Re-targeting a now-entered target appends nothing — the entry is unique.
        assert len(diags) == 1
        assert diags[0]["gate"] == "DESIGN_GATE"
        assert diags[0]["stage"] == "DESIGN"
        assert diags[0]["target"] == "EXECUTE"


# ── 9b. transition_stage outcome verbs + claim-strength vocabulary ───────────
# Phase-1 A-class fixes: the seven outcome verbs (ADVANCED / ALREADY_AT_TARGET /
# NOT_READY / GATE_BLOCKED / CONFLICT / ILLEGAL / ERROR), the turn-convergence contract
# (only a committed ADVANCED asks the runtime to stop), the canonical Claim strength set
# with report-style aliases, and the QUALITY_GATE scorecard severity split.
class TestTransitionOutcomes:
    @staticmethod
    def _svc(env) -> ResearchService:
        return ResearchService(drive=env.drive, scratch_root=env.scratch)

    @staticmethod
    def _project(env, pid: str) -> dict:
        return ResearchService._load_json(env.scratch / str(USER) / pid / "project.json", None)

    @staticmethod
    def _fast_stage(env, pid: str, stage: str) -> None:
        TestTransitionOutcomes._svc(env).atomic_update_project(
            USER, pid, lambda p: p.update(stage=stage)
        )

    async def test_advanced_stops_the_bound_turn(self, env):
        # The only outcome that ends the turn: a committed ADVANCED calls the generic
        # AgentTurn.request_stop contract (loop honours it at the step boundary).
        pid = (await _create_project(env.runtime, execution_mode="progressive"))["project_id"]
        self._fast_stage(env, pid, "DESIGN")
        turn = AgentTurn(user_msg="t")
        bind_turn(turn)
        try:
            res = await _run(env.runtime, "research_state", action="transition_stage",
                             project_id=pid, target="EXECUTE")
        finally:
            bind_turn(None)
        assert res["transition"] == "ADVANCED" and res["granted"] is True
        assert turn.stop_requested is True
        assert turn.stop_reason == "stage_advanced"

    async def test_non_advanced_outcomes_never_stop_the_turn(self, env):
        # ILLEGAL / CONFLICT / ALREADY_AT_TARGET leave the turn running.
        pid = (await _create_project(env.runtime))["project_id"]
        turn = AgentTurn(user_msg="t")
        bind_turn(turn)
        try:
            illegal = await _run(env.runtime, "research_state", action="transition_stage",
                                 project_id=pid, target="WRITE")
            conflict = await _run(env.runtime, "research_state", action="transition_stage",
                                  project_id=pid, target="FRAME",
                                  expected_current_stage="EVIDENCE")
            at_target = await _run(env.runtime, "research_state", action="transition_stage",
                                   project_id=pid, target="DISCOVER")
        finally:
            bind_turn(None)
        assert illegal["transition"] == "ILLEGAL" and illegal["granted"] is False
        assert conflict["transition"] == "CONFLICT" and conflict["granted"] is False
        assert conflict["stage"] == "DISCOVER"
        assert at_target["transition"] == "ALREADY_AT_TARGET" and at_target["granted"] is True
        assert turn.stop_requested is False
        assert self._project(env, pid)["stage"] == "DISCOVER"  # nothing written

    async def test_strict_splits_not_ready_from_gate_blocked(self, env):
        pid = (await _create_project(env.runtime))["project_id"]  # strict default
        self._fast_stage(env, pid, "DESIGN")
        never = await _run(env.runtime, "research_state", action="transition_stage",
                           project_id=pid, target="EXECUTE")
        assert never["transition"] == "NOT_READY" and never["granted"] is False
        assert never["gate"] == "DESIGN_GATE"
        checked = await _run(env.runtime, "research_gate", action="check",
                             project_id=pid, gate_name="DESIGN_GATE")
        assert checked["status"] == "FAIL"
        blocked = await _run(env.runtime, "research_state", action="transition_stage",
                             project_id=pid, target="EXECUTE")
        assert blocked["transition"] == "GATE_BLOCKED" and blocked["granted"] is False
        assert self._project(env, pid)["stage"] == "DESIGN"

    async def test_unknown_stage_is_illegal(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        res = await _run(env.runtime, "research_state", action="transition_stage",
                         project_id=pid, target="NOPE")
        assert res["transition"] == "ILLEGAL" and "unknown" in res["reason"]


class TestClaimStrengthVocabulary:
    @staticmethod
    def _svc(env) -> ResearchService:
        return ResearchService(drive=env.drive, scratch_root=env.scratch)

    async def test_record_node_normalizes_report_style_aliases(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        for raw, canonical in (("high", "confident"), ("Medium", "supported"), ("low", "asserted")):
            res = await _run(env.runtime, "research_evidence", action="record_node",
                             project_id=pid,
                             node={"id": f"C{raw}", "type": "Claim", "label": "c", "strength": raw})
            assert res["node"]["strength"] == canonical, raw

    async def test_record_node_rejects_illegal_strength_with_repair_hint(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        result = await env.runtime.execute(ToolExecution(
            call_id=str(uuid.uuid4()), name="research_evidence",
            arguments={"action": "record_node", "project_id": pid,
                       "node": {"id": "C", "type": "Claim", "label": "c", "strength": "banana"}},
        ))
        assert result.is_error is True
        msg = result.error.message
        assert "banana" in msg and "confident" in msg and "normalized" in msg

    async def test_mutate_node_patch_is_normalized_and_guarded(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        await _run(env.runtime, "research_evidence", action="record_node", project_id=pid,
                   node={"id": "C", "type": "Claim", "label": "c", "strength": "asserted"})
        patched = await _run(env.runtime, "research_evidence", action="mutate_node",
                             project_id=pid, node_id="C", patch={"strength": "high"})
        assert patched["node"]["strength"] == "confident"
        bad = await env.runtime.execute(ToolExecution(
            call_id=str(uuid.uuid4()), name="research_evidence",
            arguments={"action": "mutate_node", "project_id": pid, "node_id": "C",
                       "patch": {"strength": "very-strong"}},
        ))
        assert bad.is_error is True and "very-strong" in bad.error.message

    async def test_claim_gate_accepts_legacy_raw_strength(self, env):
        # Read-side compat: strengths stored before the alias table (raw "high") still
        # score valid in CLAIM_GATE.
        pid = (await _create_project(env.runtime))["project_id"]
        TestTransitionOutcomes._fast_stage(env, pid, "WRITE")
        res = await _run(env.runtime, "research_evidence", action="record_node", project_id=pid,
                         node={"id": "C", "type": "Claim", "label": "c", "strength": "high",
                               "citations": ["u1"]})
        assert res["node"]["strength"] == "confident"
        # Rewind the stored node to the pre-fix raw value, as a legacy graph would have it.
        svc = self._svc(env)
        graph = svc._load_graph(USER, pid)
        next(n for n in graph["nodes"] if n["id"] == "C")["strength"] = "high"
        svc._save_graph(USER, pid, graph)
        gate = await _run(env.runtime, "research_gate", action="check",
                          project_id=pid, gate_name="CLAIM_GATE")
        assert gate["status"] == "PASS"

    async def test_quality_gate_scorecard_missing_is_diagnostic_only(self, env):
        pid = (await _create_project(env.runtime, execution_mode="progressive"))["project_id"]
        gate = await _run(env.runtime, "research_gate", action="check",
                          project_id=pid, gate_name="QUALITY_GATE")
        assert gate["status"] == "FAIL"
        sc = next(c for c in gate["checks"] if c["name"] == "scorecard")
        assert sc["ok"] is False and sc["severity"] == "diagnostic_only"
        assert "scorecard_missing" in sc["detail"]

    async def test_quality_gate_present_scorecard_is_blocking(self, env):
        pid = (await _create_project(env.runtime))["project_id"]
        rows = [{"metric": f"m{i}", "ok": True} for i in range(7)]
        self._svc(env).atomic_update_project(USER, pid, lambda p: p.update(scorecard=rows))
        gate = await _run(env.runtime, "research_gate", action="check",
                          project_id=pid, gate_name="QUALITY_GATE")
        assert gate["status"] == "PASS"
        sc = next(c for c in gate["checks"] if c["name"] == "scorecard")
        assert sc["severity"] == "blocking"


# ── 10. Versioned chat-task run output layout ────────────────────────────────
# Red lines of the output-layout plan: atomic per-run ``run_seq`` stamped into the driver
# (temp/vN mirror + outputs/<stem>_vN promote), a fresh ``cloud_assets`` ledger per run,
# Create-New versioned finals that never reuse another run's assets, silent save_scrape
# capture under temp/v{N}/scrape/, and the append-only run_events.json drained by the worker.
class TestRunOutputLayout:
    @staticmethod
    def _svc(env) -> ResearchService:
        return ResearchService(drive=env.drive, scratch_root=env.scratch)

    @staticmethod
    def _project(env, task_id: str) -> dict:
        return ResearchService._load_json(env.scratch / str(USER) / task_id / "project.json", None)

    @staticmethod
    async def _new_task(env, title: str = "task") -> tuple[ResearchService, str, str]:
        svc = TestRunOutputLayout._svc(env)
        created = await svc.create_task(USER, title=title)
        return svc, created["task_id"], created["cloud_folder_path"]

    @staticmethod
    async def _files_in(env, folder_path: str) -> list[dict]:
        files = await env.drive.list_files(USER)
        return [f for f in files if (f["folder_path"] or "") == folder_path]

    async def test_begin_run_mints_run_seq_and_resets_driver_per_run(self, env):
        svc, task_id, _ = await self._new_task(env)
        project = self._project(env, task_id)
        assert project["run_seq"] == 0
        assert project["driver"] is None

        run1 = svc.begin_run(USER, task_id)
        p1 = self._project(env, task_id)
        assert p1["run_seq"] == 1
        assert p1["driver"]["run_id"] == run1["run_id"]
        assert p1["driver"]["run_version"] == 1
        assert p1["driver"]["cloud_assets"] == {}
        assert p1["driver"]["progress_cursor"] == 0

        svc.end_run(USER, task_id)
        run2 = svc.begin_run(USER, task_id)
        p2 = self._project(env, task_id)
        assert p2["run_seq"] == 2
        assert p2["driver"]["run_id"] == run2["run_id"]
        assert p2["driver"]["run_version"] == 2
        # The transient per-run ledger is reset by every begin_run (never shared across runs).
        assert p2["driver"]["cloud_assets"] == {}
        assert p2["driver"]["progress_cursor"] == 0
        svc.end_run(USER, task_id)

    async def test_begin_run_default_keeps_a_finished_task_untouched(self, env):
        # A plain begin_run (a typed resume message, no Run control) on a PUBLISHed task must
        # NOT restart it — stage/gates/diagnostics/graph stay, so casual chat can't burn a
        # full re-run. It only bumps the version counter (the no-op resume behaviour).
        svc, task_id, _ = await self._new_task(env)
        project = self._project(env, task_id)
        project["run_seq"] = 1
        project["stage"] = "PUBLISH"
        project["gates"] = {g: "FAIL" for g in _GATES}
        project["diagnostics"] = [{"gate": "EVIDENCE_GATE", "stage": "EXECUTE", "target": "EXPLAIN"}]
        project["last_block"] = {"kind": "finished", "reason": "research task reached PUBLISH"}
        svc._save_json(svc._project_dir(USER, task_id) / "project.json", project)
        svc._save_graph(USER, task_id, {"nodes": [{"id": "src:old"}], "edges": []})

        svc.begin_run(USER, task_id)
        p = self._project(env, task_id)
        assert p["run_seq"] == 2
        assert p["stage"] == "PUBLISH"
        assert p["gates"] == {g: "FAIL" for g in _GATES}
        assert p["diagnostics"] != []
        graph = svc._load_graph(USER, task_id)
        assert len(graph["nodes"]) == 1  # evidence kept

    async def test_begin_run_new_edition_resets_a_finished_task(self, env):
        # The desktop Run control on a task that already reached PUBLISH starts a NEW edition:
        # stage -> DISCOVER, gates -> NOT_RUN, diagnostics + last_block cleared, evidence graph
        # emptied — so the driver actually drives DISCOVER→…→PUBLISH again into run_seq+1
        # (temp/v2 + outputs/_v2). Versioned history of the prior edition is untouched.
        svc, task_id, _ = await self._new_task(env)
        project = self._project(env, task_id)
        project["run_seq"] = 1
        project["stage"] = "PUBLISH"
        project["gates"] = {g: "FAIL" for g in _GATES}
        project["diagnostics"] = [{"gate": "EVIDENCE_GATE", "stage": "EXECUTE",
                                   "target": "EXPLAIN", "failed_checks": []}]
        project["last_block"] = {"kind": "finished", "reason": "research task reached PUBLISH",
                                 "at": "t", "run_id": "r", "execution_id": "e"}
        svc._save_json(svc._project_dir(USER, task_id) / "project.json", project)
        svc._save_graph(USER, task_id, {"nodes": [{"id": "src:old"}], "edges": [{"src": "s", "dst": "d"}]})

        run = svc.begin_run(USER, task_id, new_edition=True)
        p = self._project(env, task_id)
        assert p["run_seq"] == 2
        assert p["driver"]["run_version"] == 2
        assert p["driver"]["run_id"] == run["run_id"]
        assert p["stage"] == "DISCOVER"
        assert p["gates"] == {g: "NOT_RUN" for g in _GATES}
        assert p["diagnostics"] == []
        assert p["last_block"] is None
        graph = svc._load_graph(USER, task_id)
        assert graph["nodes"] == [] and graph["edges"] == []

    async def test_begin_run_new_edition_is_a_noop_mid_chain(self, env):
        # new_edition only restarts a FINISHED task. A mid-chain (blocked/resume) task keeps
        # its stage + evidence so the Run control resumes exactly where it stopped.
        svc, task_id, _ = await self._new_task(env)
        project = self._project(env, task_id)
        project["run_seq"] = 1
        project["stage"] = "EVIDENCE"
        project["gates"] = {g: "NOT_RUN" for g in _GATES}
        project["last_block"] = {"kind": "blocked", "reason": "human override pending"}
        svc._save_json(svc._project_dir(USER, task_id) / "project.json", project)
        svc._save_graph(USER, task_id, {"nodes": [{"id": "src:live"}], "edges": []})

        svc.begin_run(USER, task_id, new_edition=True)
        p = self._project(env, task_id)
        assert p["run_seq"] == 2
        assert p["stage"] == "EVIDENCE"  # resumes, never restarted
        graph = svc._load_graph(USER, task_id)
        assert len(graph["nodes"]) == 1  # mid-run evidence kept

    async def test_begin_run_falls_back_for_legacy_task_without_run_seq(self, env):
        svc, task_id, _ = await self._new_task(env)
        project = self._project(env, task_id)
        del project["run_seq"]  # a task persisted before this feature
        project["driver"] = None
        svc._save_json(svc._project_dir(USER, task_id) / "project.json", project)
        svc.begin_run(USER, task_id)
        p = self._project(env, task_id)
        assert p["run_seq"] == 1
        assert p["driver"]["run_version"] == 1

    async def test_write_scratch_mirrors_into_temp_vN_and_updates_in_place_same_run(self, env):
        svc, task_id, cloud_root = await self._new_task(env)
        svc.begin_run(USER, task_id)
        written = await svc.write_scratch(USER, task_id, artifact_id="plan", content="# Plan v1")
        assert written["version"] == 1

        # The working copy mirrors into temp/v1 — never into outputs/ (that is promote's job).
        v1_files = await self._files_in(env, f"{cloud_root}/temp/v1")
        assert len(v1_files) == 1
        plan = v1_files[0]
        assert plan["name"] == "plan.md"
        assert await env.drive.read_text(USER, uuid.UUID(plan["id"])) == "# Plan v1"
        assert await self._files_in(env, f"{cloud_root}/outputs") == []

        # The record never carries the run's temp id: the per-run ledger owns it, so one run's
        # asset id can never leak into a later run's bookkeeping.
        record = ResearchService._load_json(
            env.scratch / str(USER) / task_id / "artifacts" / "plan" / "v1", None
        )
        assert record["cloud_output_asset_id"] is None
        ledger = self._project(env, task_id)["driver"]["cloud_assets"]
        assert {"temp", "temp/v1"} <= set(ledger["_dirs"])
        assert ledger["_a:plan"]["temp_asset"] == plan["id"]

        # A new scratch version inside the SAME run refreshes that one temp file in place.
        await svc.create_version(USER, task_id, artifact_id="plan", content="# Plan v1b")
        v1_files = await self._files_in(env, f"{cloud_root}/temp/v1")
        assert len(v1_files) == 1
        assert v1_files[0]["id"] == plan["id"]
        assert await env.drive.read_text(USER, uuid.UUID(plan["id"])) == "# Plan v1b"
        svc.end_run(USER, task_id)

    async def test_next_run_mirrors_to_fresh_temp_v2_and_keeps_v1(self, env):
        svc, task_id, cloud_root = await self._new_task(env)
        svc.begin_run(USER, task_id)
        await svc.write_scratch(USER, task_id, artifact_id="plan", content="# v1 body")
        v1_asset = (await self._files_in(env, f"{cloud_root}/temp/v1"))[0]["id"]
        svc.end_run(USER, task_id)

        # Run 2 starts a brand-new ledger and stamps temp/v2 (never touches v1's files).
        svc.begin_run(USER, task_id)
        await svc.write_scratch(USER, task_id, artifact_id="plan", content="# v2 body")
        v2_files = await self._files_in(env, f"{cloud_root}/temp/v2")
        assert len(v2_files) == 1
        assert v2_files[0]["name"] == "plan.md"
        assert v2_files[0]["id"] != v1_asset  # physical isolation: no cross-run asset reuse
        assert await env.drive.read_text(USER, uuid.UUID(v2_files[0]["id"])) == "# v2 body"
        assert await env.drive.read_text(USER, uuid.UUID(v1_asset)) == "# v1 body"

        ledger = self._project(env, task_id)["driver"]["cloud_assets"]
        assert set(ledger["_dirs"]) == {"temp", "temp/v2"}  # v1's dir is not in run 2's ledger
        assert ledger["_a:plan"]["temp_asset"] == v2_files[0]["id"]
        svc.end_run(USER, task_id)

    async def test_temp_folder_created_once_per_run(self, env):
        # get-or-create (red line 7): several artifacts in one run reuse the single temp/vN
        # folder row; a duplicate same-name folder is never minted per write.
        svc, task_id, cloud_root = await self._new_task(env)
        svc.begin_run(USER, task_id)
        await svc.write_scratch(USER, task_id, artifact_id="plan", content="# p")
        await svc.write_scratch(USER, task_id, artifact_id="notes", content="# n")
        folders = await env.drive.list_folders(USER)
        assert len([f for f in folders if f["path"] == f"{cloud_root}/temp/v1"]) == 1
        svc.end_run(USER, task_id)

    async def test_promote_mints_outputs_vN_and_single_run_is_strongly_idempotent(self, env):
        svc, task_id, cloud_root = await self._new_task(env)
        svc.begin_run(USER, task_id)
        # ``draft.md`` -> ``draft_v1.md`` (the raw stem, not ``draft.md_v1.md``).
        await svc.write_scratch(USER, task_id, artifact_id="draft.md", content="# final v1")

        promoted = await svc.promote_to_drive(USER, task_id, artifact_id="draft.md")
        assert promoted["status"] == "PROMOTED"
        assert promoted["rag_status"] == "PENDING"
        assert promoted["drive_path"] == f"{cloud_root}/outputs/draft_v1.md"
        outs = await self._files_in(env, f"{cloud_root}/outputs")
        assert [a["name"] for a in outs] == ["draft_v1.md"]
        out_id = outs[0]["id"]
        assert out_id == promoted["drive_asset_id"]
        assert await env.drive.read_text(USER, uuid.UUID(out_id)) == "# final v1"
        # The temp working copy is intact — promotion never moves/renames it (red line 2).
        # (The mirror keeps the raw artifact id + ``.md``, so ``draft.md`` -> ``draft.md.md``.)
        assert {a["name"] for a in await self._files_in(env, f"{cloud_root}/temp/v1")} == {
            "draft.md.md"
        }

        # Re-promote without changes is a record-level no-op (no second _v1 file).
        again = await svc.promote_to_drive(USER, task_id, artifact_id="draft.md")
        assert again["idempotent"] is True
        assert again["drive_asset_id"] == promoted["drive_asset_id"]
        assert len(await self._files_in(env, f"{cloud_root}/outputs")) == 1

        # A genuinely-new version in the SAME run refreshes the one _v1 final in place: the
        # physical asset and filename never bump to _v2 (only a new begin_run can).
        await svc.create_version(USER, task_id, artifact_id="draft.md", content="# final v2")
        repromote = await svc.promote_to_drive(USER, task_id, artifact_id="draft.md")
        outs = await self._files_in(env, f"{cloud_root}/outputs")
        assert [a["name"] for a in outs] == ["draft_v1.md"]
        assert outs[0]["id"] == out_id
        assert repromote["drive_path"] == f"{cloud_root}/outputs/draft_v1.md"
        assert await env.drive.read_text(USER, uuid.UUID(out_id)) == "# final v2"
        svc.end_run(USER, task_id)

    async def test_second_run_promotes_to_outputs_v2_keeping_v1(self, env):
        svc, task_id, cloud_root = await self._new_task(env)
        svc.begin_run(USER, task_id)
        await svc.write_scratch(USER, task_id, artifact_id="report", content="# final v1")
        run1 = await svc.promote_to_drive(USER, task_id, artifact_id="report")
        svc.end_run(USER, task_id)

        svc.begin_run(USER, task_id)  # run_seq 2
        await svc.write_scratch(USER, task_id, artifact_id="report", content="# final v2")
        run2 = await svc.promote_to_drive(USER, task_id, artifact_id="report")
        assert run2["drive_path"] == f"{cloud_root}/outputs/report_v2.md"
        assert run2["drive_asset_id"] != run1["drive_asset_id"]

        outs = {a["name"]: a for a in await self._files_in(env, f"{cloud_root}/outputs")}
        assert set(outs) == {"report_v1.md", "report_v2.md"}
        # v1 is never overwritten or reused — both versioned finals coexist with their bytes.
        assert await env.drive.read_text(USER, uuid.UUID(outs["report_v1.md"]["id"])) == "# final v1"
        assert await env.drive.read_text(USER, uuid.UUID(outs["report_v2.md"]["id"])) == "# final v2"
        svc.end_run(USER, task_id)

    async def test_save_scrape_writes_metadata_file_and_increments_per_run(self, env):
        svc, task_id, cloud_root = await self._new_task(env)
        # Outside a versioned run save_scrape is a silent no-op, not an error.
        assert await svc.save_scrape(
            USER, task_id, source="web", url="https://x", query="q", content="body"
        ) == {"saved": False, "path": None, "reason": "no versioned cloud task run"}

        svc.begin_run(USER, task_id)
        res = await svc.save_scrape(
            USER, task_id,
            source="web", url="https://example.com/a",
            query='how to "cook"/ *tomato*', content="page body",
        )
        assert res["saved"] is True
        assert res["path"] == f"{cloud_root}/temp/v1/scrape/web_how_to_cook_tomato_1.md"
        text = await env.drive.read_text(USER, uuid.UUID(res["asset_id"]))
        assert "Source: web" in text
        assert "URL: https://example.com/a" in text
        assert 'Query: how to "cook"/ *tomato*' in text
        assert "Run_version: 1" in text
        assert "Retrieved_at:" in text
        assert text.endswith("page body")

        # A second save for the same source+query increments its seq — never overwrites.
        res2 = await svc.save_scrape(
            USER, task_id,
            source="web", url="https://example.com/b",
            query='how to "cook"/ *tomato*', content="second page",
        )
        assert res2["path"] == f"{cloud_root}/temp/v1/scrape/web_how_to_cook_tomato_2.md"
        scrapes = await self._files_in(env, f"{cloud_root}/temp/v1/scrape")
        assert {a["name"] for a in scrapes} == {
            "web_how_to_cook_tomato_1.md", "web_how_to_cook_tomato_2.md"
        }
        # Capture is silent: no run event, no chat line (totals fold into stage summaries).
        events = ResearchService._load_json(
            svc._run_events_path(USER, task_id), {"events": []}
        )["events"]
        assert events == []
        svc.end_run(USER, task_id)

    async def test_run_events_append_only_dedupe_and_drain(self, env, monkeypatch):
        svc, task_id, _ = await self._new_task(env)
        session = uuid.uuid4()
        svc.bind_session(USER, task_id, session)

        # Outside a run nothing is logged (skills / plain projects have no run_seq).
        assert svc.append_run_event(
            USER, task_id, event_type="stage", key="stage:FRAME", detail="x"
        ) is None

        svc.begin_run(USER, task_id)
        seq1 = svc.append_run_event(
            USER, task_id, event_type="stage", key="stage:FRAME",
            detail="granted DISCOVER -> FRAME", stage="DISCOVER",
        )
        seq2 = svc.append_run_event(
            USER, task_id, event_type="gate_diagnostic", key="gate:EVIDENCE_GATE",
            detail="2 checks failed", stage="EXECUTE",
        )
        # (run_seq, type, key) is appended at most once — replay returns the original seq.
        assert svc.append_run_event(
            USER, task_id, event_type="stage", key="stage:FRAME",
            detail="granted DISCOVER -> FRAME", stage="DISCOVER",
        ) == seq1

        path = svc._run_events_path(USER, task_id)
        data = ResearchService._load_json(path, {"events": []})
        events = data["events"]
        assert [e["seq"] for e in events] == [seq1, seq2]
        assert len({e["event_id"] for e in events}) == 2  # unique ids, append-only
        assert all(e["run_seq"] == 1 for e in events)
        assert events[0]["type"] == "stage" and events[1]["type"] == "gate_diagnostic"

        # Worker drain: each un-consumed event becomes ONE system row (role=system rows are
        # filtered from model context everywhere), then the cursor advances past it.
        inserted = []

        async def fake_insert(session_factory, user_id, sid, role, text):
            inserted.append((str(user_id), str(sid), role, text))

        monkeypatch.setattr("core.infrastructure.memory.insert_plain_message", fake_insert)
        written = await svc.drain_run_events(object(), USER, task_id, str(session))
        assert written == 2
        assert [(r[2], r[3]) for r in inserted] == [
            ("system", "[auto-run v1] stage → DISCOVER: granted DISCOVER -> FRAME"),
            ("system", "[auto-run v1] gate recorded: 2 checks failed"),
        ]
        assert all(r[0] == str(USER) and r[1] == str(session) for r in inserted)
        # Each system row is also mirrored into the bound task's session transcript.
        mirror = ResearchService._load_json(
            env.scratch / str(USER) / task_id / "session_history.json", None
        )
        assert [t["role"] for t in mirror["turns"]] == ["system", "system"]

        # Cursor advanced: a second drain emits nothing new.
        assert await svc.drain_run_events(object(), USER, task_id, str(session)) == 0
        assert self._project(env, task_id)["driver"]["progress_cursor"] == seq2
        svc.end_run(USER, task_id)

    async def test_drain_db_failure_keeps_cursor_before_failed_row(self, env, monkeypatch):
        svc, task_id, _ = await self._new_task(env)
        session = uuid.uuid4()
        svc.bind_session(USER, task_id, session)
        svc.begin_run(USER, task_id)
        s1 = svc.append_run_event(USER, task_id, event_type="stage", key="stage:FRAME", detail="a")
        s2 = svc.append_run_event(
            USER, task_id, event_type="artifact", key="artifact:report", detail="b"
        )

        calls = {"n": 0}

        async def flaky(session_factory, user_id, sid, role, text):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db down")

        monkeypatch.setattr("core.infrastructure.memory.insert_plain_message", flaky)
        # s1 is committed (written=1) but the DB dies on s2, so the cursor stops BEFORE it.
        assert await svc.drain_run_events(object(), USER, task_id, str(session)) == 1
        assert self._project(env, task_id)["driver"]["progress_cursor"] == s1

        # DB recovers: the retry emits only the failed row, never duplicating s1.
        drained = []

        async def healthy(session_factory, user_id, sid, role, text):
            drained.append(text)

        monkeypatch.setattr("core.infrastructure.memory.insert_plain_message", healthy)
        assert await svc.drain_run_events(object(), USER, task_id, str(session)) == 1
        assert drained == ["[auto-run v1] b"]
        assert self._project(env, task_id)["driver"]["progress_cursor"] == s2
        svc.end_run(USER, task_id)

    async def test_drain_skips_leftover_events_from_an_older_run(self, env, monkeypatch):
        svc, task_id, _ = await self._new_task(env)
        session = uuid.uuid4()
        svc.bind_session(USER, task_id, session)
        svc.begin_run(USER, task_id)
        svc.append_run_event(USER, task_id, event_type="stage", key="stage:FRAME", detail="old-run")
        svc.end_run(USER, task_id)
        svc.begin_run(USER, task_id)  # run_seq 2; its own events have not been written yet

        inserted = []

        async def fake_insert(session_factory, user_id, sid, role, text):
            inserted.append(text)

        monkeypatch.setattr("core.infrastructure.memory.insert_plain_message", fake_insert)
        # Old-run leftovers are never shown again, but the cursor still consumes them.
        assert await svc.drain_run_events(object(), USER, task_id, str(session)) == 0
        assert inserted == []
        assert self._project(env, task_id)["driver"]["progress_cursor"] == 1
        svc.end_run(USER, task_id)


# ── 11. Batch EVIDENCE: fetch / verify / read (E1-E3, E7-E10) ─────────────────
class TestBatchEvidence:
    """Wholesale EVIDENCE through the real fetch + provenance + verify path.

    The E-series trust boundaries folded into the batch flow: E3 code-level batch cap,
    E7 provenance-gated verify (server ledger is authoritative), E8 Claim immutability,
    E9 read-back authz, E10 concurrent-save isolation — plus E1 (one (Claim, canonical
    URL) is exactly one Evidence) and E2 (``neutral`` is not a ticket).
    """

    _PUBLIC = "93.184.216.34"

    @staticmethod
    def _svc(env) -> ResearchService:
        return ResearchService(drive=env.drive, scratch_root=env.scratch)

    @staticmethod
    def _project(env, task_id: str) -> dict:
        return ResearchService._load_json(
            env.scratch / str(USER) / task_id / "project.json", None
        )

    @staticmethod
    def _graph(env, task_id: str) -> dict:
        return ResearchService._load_json(
            env.scratch / str(USER) / task_id / "graph.json", {"nodes": [], "edges": []}
        )

    @staticmethod
    def _provenance(env, task_id: str) -> dict:
        p = TestBatchEvidence._project(env, task_id)
        return p["driver"]["cloud_assets"].get("_fetch_provenance", {})

    @staticmethod
    async def _new_task(env) -> tuple[ResearchService, str]:
        svc = TestBatchEvidence._svc(env)
        created = await svc.create_task(USER, title="batch evidence")
        return svc, created["task_id"]

    @staticmethod
    def _article(fact: str, n: int = 12) -> str:
        paras = "".join(
            f"<p>{fact} sentence {i}: the cleaned article text must comfortably clear "
            "the usable floor so the page counts as a verifiable source.</p>"
            for i in range(n)
        )
        return f"<html><head><title>{fact}</title></head><body><nav>sidebar-menu</nav>{paras}</body></html>"

    @staticmethod
    def _page_handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "good.example":
            return httpx.Response(200, text=TestBatchEvidence._article("unique-fact-0001"))
        if host == "boom.example":
            return httpx.Response(200, text=TestBatchEvidence._article("unique-fact-0002"))
        if host == "empty.example":
            return httpx.Response(200, text="<html><body><p>nothing much here</p></body></html>")
        if host == "wall.example":
            return httpx.Response(
                200,
                text="<html><head><title>Sign in to continue</title></head><body>"
                "<p>please log in to read the full article</p></body></html>",
            )
        return httpx.Response(404, text="nope")

    def _install_fetch(self, monkeypatch, handler=_page_handler.__func__):
        import plugins.research.plugin as rplugin

        monkeypatch.setattr(rplugin, "_FETCH_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
        monkeypatch.setattr(rplugin, "_FETCH_RESOLVER_OVERRIDE", lambda host: [self._PUBLIC])

    # ── research_scrape fetch (E3/E7/E10) ───────────────────────────────────
    async def test_fetch_hard_caps_batch_at_three(self, env):
        svc, task_id = await self._new_task(env)
        urls = [f"https://h{i}.example/x" for i in range(4)]
        with pytest.raises(ValueError, match="at most 3 URLs"):
            await svc.fetch_save_batch(USER, task_id, urls=urls)

        result = await env.runtime.execute(
            ToolExecution(
                call_id=str(uuid.uuid4()),
                name="research_scrape",
                arguments={"action": "fetch", "project_id": task_id, "urls": urls},
            )
        )
        assert result.is_error is True
        assert "at most 3 URLs" in result.error.message

    async def test_fetch_saves_usable_only_records_ledger_and_is_silent(self, env, monkeypatch):
        svc, task_id = await self._new_task(env)
        svc.begin_run(USER, task_id)
        self._install_fetch(monkeypatch)

        views = await svc.fetch_save_batch(
            USER, task_id,
            urls=[
                "https://good.example/recipe",
                "https://empty.example/x",
                "https://wall.example/y",
            ],
        )
        by_cu = {v["canonical_url"]: v for v in views}
        assert set(by_cu) == {
            "https://good.example/recipe", "https://empty.example/x", "https://wall.example/y"
        }

        good = by_cu["https://good.example/recipe"]
        assert good["status"] == "ok"
        assert good["content_status"] == "usable"
        assert good["saved"] is True
        assert good["full_char_len"] >= MIN_FETCH_TEXT_CHARS
        assert good["asset_id"] and good["path"] and good["name"]

        empty = by_cu["https://empty.example/x"]
        assert empty["status"] == "ok"
        assert empty["content_status"] == "empty"
        assert empty["saved"] is False
        wall = by_cu["https://wall.example/y"]
        assert wall["status"] == "ok"
        assert wall["content_status"] == "interstitial"
        assert wall["saved"] is False

        # E4: the model-facing batch (body already stripped) serializes inside the cap.
        assert len(json.dumps(views, ensure_ascii=False)) <= 7200

        # E7: the run's provenance ledger records every fetched page with its real verdicts.
        prov = self._provenance(env, task_id)
        assert set(prov) == set(by_cu)
        g = prov["https://good.example/recipe"]
        assert g["fetch_status"] == "ok" and g["content_status"] == "usable"
        assert g["full_char_len"] >= MIN_FETCH_TEXT_CHARS and g["saved"] is True
        assert g["asset_id"] == good["asset_id"]
        e = prov["https://empty.example/x"]
        assert e["fetch_status"] == "ok" and e["content_status"] == "empty" and e["saved"] is False

        # E10/E6: the saved draft really holds the cleaned page (nav stripped), on the drive.
        stored = await env.drive.read_text(USER, uuid.UUID(good["asset_id"]))
        assert "unique-fact-0001" in stored
        assert "sidebar-menu" not in stored

        # Silent by contract: capture folds into stage summaries, never a run event / node.
        events = ResearchService._load_json(
            svc._run_events_path(USER, task_id), {"events": []}
        )["events"]
        assert events == []
        assert self._graph(env, task_id) == {"nodes": [], "edges": []}
        svc.end_run(USER, task_id)

    async def test_fetch_one_failed_drive_write_never_polls_ledger(self, env, monkeypatch):
        svc, task_id = await self._new_task(env)
        svc.begin_run(USER, task_id)
        self._install_fetch(monkeypatch)

        from core.application import drive_service as drive_mod

        orig = drive_mod.DriveService.save_artifact

        async def flaky(self, user_id, name, mime_type, content, *, folder_path=None,
                        workspace_id=None, source_asset_id=None):
            if name.startswith("fetch_boom"):
                raise RuntimeError("drive down mid-batch")
            return await orig(self, user_id, name, mime_type, content,
                              folder_path=folder_path, workspace_id=workspace_id,
                              source_asset_id=source_asset_id)

        monkeypatch.setattr(drive_mod.DriveService, "save_artifact", flaky)
        views = await svc.fetch_save_batch(
            USER, task_id,
            urls=["https://good.example/recipe", "https://boom.example/big"],
        )
        by_cu = {v["canonical_url"]: v for v in views}
        assert by_cu["https://good.example/recipe"]["saved"] is True
        boom = by_cu["https://boom.example/big"]
        assert boom["saved"] is False
        assert boom["reason"] == "drive write failed"

        # E10: the failed save is absent from the ledger; only successes are recorded.
        prov = self._provenance(env, task_id)
        assert set(prov) == {"https://good.example/recipe"}
        assert "https://boom.example/big" not in prov

        cloud_root = self._project(env, task_id)["cloud_folder_path"]
        stored = [f for f in await env.drive.list_files(USER)
                  if f["folder_path"] == f"{cloud_root}/temp/v1/scrape"]
        assert {a["name"].split("_", 1)[1].split(".", 1)[0] for a in stored} == {"good"}
        svc.end_run(USER, task_id)

    # ── research_scrape read (E9) ──────────────────────────────────────────────
    async def test_read_returns_full_draft_only_for_this_runs_fetch(self, env, monkeypatch):
        svc, task_id = await self._new_task(env)
        svc.begin_run(USER, task_id)
        self._install_fetch(monkeypatch)
        (view,) = await svc.fetch_save_batch(
            USER, task_id, urls=["https://good.example/recipe"]
        )
        cu, asset_id, name = view["canonical_url"], view["asset_id"], view["name"]
        stored = await env.drive.read_text(USER, uuid.UUID(asset_id))
        expected = ResearchService._scrape_body(stored)
        assert "unique-fact-0001" in expected

        by_cu = await svc.read_fetch(USER, task_id, canonical_url=cu)
        by_id = await svc.read_fetch(USER, task_id, asset_id=asset_id)
        by_name = await svc.read_fetch(USER, task_id, name=name)
        for r in (by_cu, by_id, by_name):
            assert r["asset_id"] == asset_id
            assert r["content"] == expected
        assert by_cu["canonical_url"] == "https://good.example/recipe"

        # An asset this run never fetched is refused — never a blind drive read.
        result = await env.runtime.execute(
            ToolExecution(
                call_id=str(uuid.uuid4()),
                name="research_scrape",
                arguments={"action": "read", "project_id": task_id,
                           "canonical_url": "https://other.example/nope"},
            )
        )
        assert result.is_error is True
        assert "no usable fetch recorded this run" in result.error.message

        with pytest.raises(ValueError, match="was not fetched this run"):
            await svc.read_fetch(USER, task_id, asset_id=str(uuid.uuid4()))
        # Traversal / path tricks are refused before any drive access.
        for bad in ("../escape", "a/../b", "sub/../x", "..", ".", "a\\b", "/abs"):
            with pytest.raises(ValueError):
                await svc.read_fetch(USER, task_id, name=bad)
        svc.end_run(USER, task_id)

    async def test_read_refuses_asset_from_an_earlier_run(self, env, monkeypatch):
        svc, task_id = await self._new_task(env)
        svc.begin_run(USER, task_id)
        self._install_fetch(monkeypatch)
        (view,) = await svc.fetch_save_batch(
            USER, task_id, urls=["https://good.example/recipe"]
        )
        cu, asset_id = view["canonical_url"], view["asset_id"]
        svc.end_run(USER, task_id)

        # A fresh run resets the provenance ledger; the old asset is out of scope.
        svc.begin_run(USER, task_id)
        with pytest.raises(ValueError, match="was not fetched this run"):
            await svc.read_fetch(USER, task_id, asset_id=asset_id)
        with pytest.raises(ValueError, match="no usable fetch recorded this run"):
            await svc.read_fetch(USER, task_id, canonical_url=cu)
        svc.end_run(USER, task_id)

    # ── research_evidence verify (E1/E2/E7/E8) ────────────────────────────────
    async def _claim_and_fetch_good(self, env, monkeypatch, task_id, claim_id="claim:trust"):
        self._install_fetch(monkeypatch)
        await _run(env.runtime, "research_evidence", action="record_node", project_id=task_id,
                   node={"id": claim_id, "type": "Claim", "label": "tomato fact",
                         "statement": "botanically, tomatoes are fruits"})
        views = await self._svc(env).fetch_save_batch(
            USER, task_id, urls=["https://good.example/recipe"]
        )
        return claim_id, views[0]["canonical_url"]

    async def test_verify_requires_an_existing_claim_and_never_edits_it(self, env, monkeypatch):
        svc, task_id = await self._new_task(env)
        svc.begin_run(USER, task_id)
        self._install_fetch(monkeypatch)

        # E8: verify never creates a Claim — a missing id is refused precisely.
        result = await env.runtime.execute(
            ToolExecution(
                call_id=str(uuid.uuid4()),
                name="research_evidence",
                arguments={"action": "verify", "project_id": task_id,
                           "claim": {"id": "claim:ghost"},
                           "findings": [{"url": "https://good.example/recipe",
                                         "verdict": "supports"}]},
            )
        )
        assert result.is_error is True
        assert "claim node not found" in result.error.message

        claim_id, cu = await self._claim_and_fetch_good(env, monkeypatch, task_id)
        out = svc.ingest_evidence(
            USER, task_id,
            claim={"id": claim_id, "label": "hijacked", "statement": "rewritten"},
            findings=[{"url": "https://good.example/recipe", "verdict": "supports",
                       "facts": ["tomatoes are fruit"], "excerpt": "the draft says so"}],
        )
        assert out["claim_id"] == claim_id
        assert out["verified_sources"] == [cu]
        assert out["rejected"] == [] and out["neutral_skipped"] == []

        graph = self._graph(env, task_id)
        claim_node = next(n for n in graph["nodes"] if n["id"] == claim_id)
        assert claim_node["statement"] == "botanically, tomatoes are fruits"
        assert claim_node["label"] == "tomato fact"  # verify never rewrote the Claim
        assert next(n for n in graph["nodes"] if n["type"] == "Source")["verification_status"] == "verified"
        assert next(n for n in graph["nodes"] if n["type"] == "Evidence")["verdict"] == "supports"
        assert sorted(e["kind"] for e in graph["edges"]) == ["depends_on", "supports"]
        svc.end_run(USER, task_id)

    async def test_verify_ignores_payload_content_claims_and_rejects_unproven(self, env, monkeypatch):
        svc, task_id = await self._new_task(env)
        svc.begin_run(USER, task_id)
        self._install_fetch(monkeypatch)
        claim_id = "claim:empty-lie"
        await _run(env.runtime, "research_evidence", action="record_node", project_id=task_id,
                   node={"id": claim_id, "type": "Claim", "label": "wall claim"})
        await svc.fetch_save_batch(USER, task_id, urls=["https://empty.example/x"])

        # The payload can claim usable/char_len all it wants — the server ledger is authoritative.
        out = svc.ingest_evidence(
            USER, task_id,
            claim={"id": claim_id},
            findings=[{"url": "https://empty.example/x", "verdict": "supports",
                       "usable": True, "content_status": "usable", "char_len": 5000}],
        )
        assert out["verified_sources"] == []
        assert len(out["rejected"]) == 1
        assert "not usable" in out["rejected"][0]["reason"]
        assert self._graph(env, task_id)["nodes"] != []  # only the Claim node exists

        # A URL this run never fetched has no provenance at all → rejected.
        out2 = svc.ingest_evidence(
            USER, task_id, claim={"id": claim_id},
            findings=[{"url": "https://never.example/z", "verdict": "contradicts"}],
        )
        assert out2["rejected"][0]["reason"].startswith("no fetch provenance this run")

        # And the honest path verifies: fetch the usable page, then supports is a ticket.
        claim_id2, cu = await self._claim_and_fetch_good(
            env, monkeypatch, task_id, claim_id="claim:real"
        )
        out3 = svc.ingest_evidence(
            USER, task_id, claim={"id": claim_id2},
            findings=[{"url": cu, "verdict": "supports"}],
        )
        assert out3["verified_sources"] == [cu]
        svc.end_run(USER, task_id)

    async def test_neutral_is_not_a_ticket_and_records_nothing_new(self, env, monkeypatch):
        svc, task_id = await self._new_task(env)
        svc.begin_run(USER, task_id)
        self._install_fetch(monkeypatch)
        claim_id, cu = await self._claim_and_fetch_good(env, monkeypatch, task_id)

        # Fresh neutral: no verified Evidence yet, so it is skipped — never a node or edge.
        out = svc.ingest_evidence(
            USER, task_id, claim={"id": claim_id},
            findings=[{"url": cu, "verdict": "neutral"}],
        )
        assert out["verified_sources"] == []
        assert out["nodes_added"] == 0 and out["nodes_updated"] == 0 and out["edges_added"] == 0
        assert len(out["neutral_skipped"]) == 1
        graph = self._graph(env, task_id)
        assert all(n["type"] == "Claim" for n in graph["nodes"])
        assert graph["edges"] == []
        svc.end_run(USER, task_id)

    async def test_verify_is_idempotent_per_claim_url(self, env, monkeypatch):
        svc, task_id = await self._new_task(env)
        svc.begin_run(USER, task_id)
        self._install_fetch(monkeypatch)
        claim_id, cu = await self._claim_and_fetch_good(env, monkeypatch, task_id)
        findings = [{"url": cu, "verdict": "supports",
                     "facts": ["tomatoes are fruit"], "excerpt": "the draft says so"}]

        first = svc.ingest_evidence(USER, task_id, claim={"id": claim_id}, findings=findings)
        assert first["nodes_added"] == 2 and first["edges_added"] == 2  # Source + Evidence

        # E1: replaying the same (Claim, canonical URL) is an upsert, never a duplicate.
        second = svc.ingest_evidence(USER, task_id, claim={"id": claim_id}, findings=findings)
        assert second["nodes_added"] == 0 and second["nodes_updated"] == 0
        assert second["edges_added"] == 0
        graph = self._graph(env, task_id)
        assert len(graph["nodes"]) == 3  # Claim + Source + Evidence
        assert len(graph["edges"]) == 2
        svc.end_run(USER, task_id)

    async def test_batch_flow_satisfies_evidence_gate(self, env, monkeypatch):
        svc, task_id = await self._new_task(env)
        svc.begin_run(USER, task_id)
        self._install_fetch(monkeypatch)
        claim_id, cu = await self._claim_and_fetch_good(env, monkeypatch, task_id)
        svc.ingest_evidence(
            USER, task_id, claim={"id": claim_id},
            findings=[{"url": cu, "verdict": "supports"}],
        )

        result = await _run(env.runtime, "research_gate", action="check",
                            project_id=task_id, gate_name="EVIDENCE_GATE")
        assert result["status"] == "PASS"
        assert all(c["ok"] for c in result["checks"])
        svc.end_run(USER, task_id)
