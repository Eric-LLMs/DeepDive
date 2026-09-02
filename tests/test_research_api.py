"""Regression tests for the converged ``/research`` task API.

Covers the things the plan asked to pin down:
1. Tenancy / authorization — an anonymous request is 401; user B cannot read user A's task
   (404 via the owner-scoped service, not a cross-tenant leak); user B cannot attach user A's
   drive asset as a material (403/404 from ``drive.download``).
2. Atomic create — one POST builds the whole task folder (materials/ outputs/ task_spec.json
   session_history.json) and a failed material copy rolls the folder back.
3. Path traversal — ``..`` / absolute segments in task/artifact ids raise ``ValueError`` in the
   service (→ 404 in the router) and never escape the owner scratch root.
4. Duplicate ``Promote to Drive`` is idempotent — same asset, no second drive write.
5. Backend-restart persistence — a fresh :class:`ResearchService` over the same scratch root
   recovers tasks, session bindings, artifacts, and graph exactly.

The router is exercised through a minimal FastAPI app (no main-app lifespan / DB) with
``require_user`` and ``get_drive_service`` overridden by fakes.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import AuthUser, require_user
from api.deps import get_drive_service
from api.routers import research as research_module
from api.routers.research import router as research_router
from core.infrastructure.db import UserRoleModel
from plugins.research.plugin import ResearchService
from tests._drive_fakes import make_drive

USER = uuid.uuid4()
USER_B = uuid.uuid4()

_FAKE_ROLE = UserRoleModel(role_id="user", role_name="User")


def _auth(user_id: uuid.UUID) -> AuthUser:
    return AuthUser(
        user_id=user_id,
        username="alice",
        display_name=None,
        role=_FAKE_ROLE,
        token_id=uuid.uuid4(),
    )


async def _fake_task_session(user_id, title):
    # The research router creates the task's dedicated chat session at creation; tests stub the
    # DB-backed factory with a fresh id per call (each task gets its own bound session).
    return str(uuid.uuid4())


def _make_client(drive, scratch, *, authed: bool = True):
    """A minimal app with only the research router; deps overridden by fakes."""
    app = FastAPI()
    app.include_router(research_router)
    app.dependency_overrides[get_drive_service] = lambda: drive
    if authed:
        app.dependency_overrides[require_user] = lambda: _auth(USER)
    # The router builds a request-local service from settings.research_scratch_dir;
    # point it at a throwaway scratch root so tests never touch the real data dir.
    research_module.settings = SimpleNamespace(research_scratch_dir=scratch)
    research_module._make_task_session = _fake_task_session
    return TestClient(app)


def _make_bob_client(drive_b, scratch):
    app = FastAPI()
    app.include_router(research_router)
    app.dependency_overrides[get_drive_service] = lambda: drive_b
    app.dependency_overrides[require_user] = lambda: _auth(USER_B)
    research_module.settings = SimpleNamespace(research_scratch_dir=scratch)
    research_module._make_task_session = _fake_task_session
    return TestClient(app)


@pytest.fixture
def env(tmp_path):
    drive = make_drive(tmp_path)
    scratch = tmp_path / "scratch"
    return SimpleNamespace(drive=drive, scratch=scratch)


# ── 1. Tenancy / authorization ──────────────────────────────────────────────
class TestTenancy:
    def test_anonymous_request_is_401(self, env):
        client = _make_client(env.drive, env.scratch, authed=False)
        assert client.get("/research/tasks").status_code == 401
        assert client.post("/research/tasks", json={"title": "x"}).status_code == 401

    def test_user_b_cannot_read_user_a_task(self, env):
        # A creates a task via the converged API.
        alice = _make_client(env.drive, env.scratch)
        created = alice.post("/research/tasks", json={"title": "A"}).json()
        task_id = created["task_id"]

        # B's client has its own drive/scratch but a different user id — the owner
        # scope comes from the authenticated principal, so B gets a 404 (not A's data).
        drive_b = make_drive(env.scratch.parent / "b")
        bob = _make_bob_client(drive_b, env.scratch)
        assert bob.get(f"/research/tasks/{task_id}").status_code == 404

        # B's task list is empty — no cross-tenant rows.
        assert bob.get("/research/tasks").json()["tasks"] == []

    async def test_user_b_cannot_attach_user_a_material(self, env):
        asset = await env.drive.save_artifact(
            USER, name="secret.txt", mime_type="text/plain", content=b"top secret"
        )
        alice = _make_client(env.drive, env.scratch)
        # Alice's own material attaches fine (single atomic POST, materials copied).
        ok = alice.post(
            "/research/tasks", json={"title": "A", "material_asset_ids": [str(asset.id)]}
        ).json()
        assert ok["materials"][0]["asset_id"] == str(asset.id)

        # Bob cannot smuggle Alice's asset into his task -> 403. He queries the SAME drive
        # (Alice's asset exists there) but is authenticated as USER_B, so drive.download
        # denies the read — the 403 proves the ownership gate, not a missing asset.
        bob = _make_bob_client(env.drive, env.scratch)
        res = bob.post("/research/tasks", json={"title": "B", "material_asset_ids": [str(asset.id)]})
        assert res.status_code == 403

        # A nonexistent asset -> 404, and nothing half-built survives.
        res = bob.post("/research/tasks", json={"title": "B", "material_asset_ids": [str(uuid.uuid4())]})
        assert res.status_code == 404
        assert bob.get("/research/tasks").json()["tasks"] == []


# ── 2. Atomic create ────────────────────────────────────────────────────────
class TestCreateTask:
    async def test_create_builds_whole_folder(self, env):
        asset = await env.drive.save_artifact(
            USER, name="paper.pdf", mime_type="application/pdf", content=b"%PDF"
        )
        client = _make_client(env.drive, env.scratch)
        created = client.post(
            "/research/tasks",
            json={"title": "VecDB", "description": "recall", "material_asset_ids": [str(asset.id)]},
        ).json()
        task_id = created["task_id"]
        assert created["stage"] == "DISCOVER"
        assert created["status"] == "ACTIVE"
        assert created["cloud_folder_path"] == "VecDB"

        # Scratch state is authoritative and complete.
        task_dir = env.scratch / str(USER) / task_id
        for f in ("project.json", "graph.json", "task_spec.json", "session_history.json"):
            assert (task_dir / f).is_file(), f
        project = ResearchService._load_json(task_dir / "project.json", None)
        assert project["cloud_folder_id"]
        assert project["cloud_folder_path"] == "VecDB"
        # Materials provenance row: {asset_id, name, cloud_asset_id, mime}.
        assert project["materials"][0]["asset_id"] == str(asset.id)
        assert project["materials"][0]["name"] == "paper.pdf"
        assert project["materials"][0]["mime"] == "application/pdf"
        assert project["materials"][0]["cloud_asset_id"]

        # Cloud projection: the task folder + material asset + the two JSON mirrors.
        folders = [f["name"] for f in await env.drive.list_folders(USER)]
        assert "VecDB" in folders
        # The two work folders always exist inside the task folder, even before any outputs.
        assert "materials" in folders and "outputs" in folders
        files = await env.drive.list_files(USER)
        mats = [a for a in files if a["folder_path"] == "VecDB/materials"]
        assert len(mats) == 1
        assert mats[0]["name"] == f"{asset.id}__paper.pdf"
        assert mats[0]["id"] == project["materials"][0]["cloud_asset_id"]

        # GET /research/tasks returns the task in the list; GET /research/tasks/{id} the status.
        assert [t["task_id"] for t in client.get("/research/tasks").json()["tasks"]] == [task_id]
        status = client.get(f"/research/tasks/{task_id}").json()
        assert status["stage"] == "DISCOVER"
        assert status["description"] == "recall"
        assert status["materials"] == [f"{asset.id}__paper.pdf"]
        # Working-directory projection: root mirrors + the material asset, with paths.
        assert {f["name"] for f in status["cloud_files"]} == {
            "task_spec.json", "session_history.json", f"{asset.id}__paper.pdf",
        }
        assert {f["folder_path"] for f in status["cloud_files"]} == {"VecDB", "VecDB/materials"}
        # Each entry carries the asset id + mime so the frontend can fetch content on click.
        assert all(f["id"] and f["mime_type"] for f in status["cloud_files"])

    async def test_create_in_subfolder(self, env):
        client = _make_client(env.drive, env.scratch)
        created = client.post(
            "/research/tasks", json={"title": "nested", "parent_folder_path": "Projects"}
        ).json()
        project = ResearchService._load_json(
            env.scratch / str(USER) / created["task_id"] / "project.json", None
        )
        # The working directory is honored: the task folder lands under Projects/.
        assert project["cloud_folder_path"] == "Projects/nested"
        folders = [f["path"] for f in await env.drive.list_folders(USER)]
        assert "Projects/nested" in folders

    def test_create_binds_a_dedicated_session(self, env):
        # Each task owns exactly one chat session, bound at creation (1:1). The same id is
        # exposed by the create response, the task list, and the task status, so the desktop
        # can open it on selection and reuse it on every switch (never forking a new session).
        client = _make_client(env.drive, env.scratch)
        created = client.post("/research/tasks", json={"title": "sess-bound"}).json()
        session_id = created["session_id"]
        assert session_id
        assert [t["session_id"] for t in client.get("/research/tasks").json()["tasks"]] == [session_id]
        assert client.get(f"/research/tasks/{created['task_id']}").json()["session_id"] == session_id

        # A second task gets its own session; both are recorded in the owner's session index.
        second = client.post("/research/tasks", json={"title": "second"}).json()
        assert second["session_id"] and second["session_id"] != session_id
        svc = ResearchService(env.drive, env.scratch)
        assert svc.bound_session_ids(USER) == {session_id, second["session_id"]}

    def test_create_requires_title(self, env):
        client = _make_client(env.drive, env.scratch)
        assert client.post("/research/tasks", json={"title": ""}).status_code == 422
        assert client.post("/research/tasks", json={}).status_code == 422


# ── 3. Path traversal (404/400, never an escape) ────────────────────────────
class TestPathTraversal:
    def test_router_traversal_is_404(self, env):
        client = _make_client(env.drive, env.scratch)
        for evil in ("../..", "..", "%2e%2e%2fevil", "a/../.."):
            res = client.get(f"/research/tasks/{evil}")
            assert res.status_code in (400, 404), evil

    def test_service_rejects_dotdot_escape(self, env):
        svc = ResearchService(env.drive, env.scratch)
        with pytest.raises(ValueError):
            svc._resolve_owned_path(USER, "..", "evil")
        with pytest.raises(ValueError):
            svc._project_dir(USER, "../evil")
        with pytest.raises(ValueError):
            svc._project_dir(USER, str(env.scratch.parent.resolve()))  # absolute segment

    async def test_service_rejects_artifact_traversal(self, env):
        svc = ResearchService(env.drive, env.scratch)
        pid = (await svc.create_task(USER, title="t"))["task_id"]
        # task_id segment climbs out of the owner root.
        with pytest.raises(ValueError):
            svc._artifact_dir(USER, "..", "x")
        # artifact_id climbs past owner_root/<pid>/artifacts out of the owner root.
        with pytest.raises(ValueError):
            svc._artifact_dir(USER, pid, "../../../evil")

    async def test_list_tasks_skips_non_task_dirs(self, env):
        svc = ResearchService(env.drive, env.scratch)
        # A stray directory at the owner root (e.g. the _session_index.json) must be skipped.
        (env.scratch / str(USER) / "stray").mkdir(parents=True)
        (env.scratch / str(USER) / "stray" / "note.txt").write_text("x", encoding="utf-8")
        await svc.create_task(USER, title="real")
        ids = [t["task_id"] for t in svc.list_tasks(USER)]
        assert len(ids) == 1  # only the real task, not the stray dir


# ── 4. Duplicate Promote idempotency ────────────────────────────────────────
class TestPromoteIdempotency:
    async def test_duplicate_promote_returns_idempotent(self, env):
        client = _make_client(env.drive, env.scratch)
        task_id = client.post("/research/tasks", json={"title": "p"}).json()["task_id"]
        # Write a draft through the service (the console never writes artifacts).
        svc = ResearchService(env.drive, env.scratch)
        await svc.write_scratch(USER, task_id, artifact_id="report", content="# Final\n\nBody.")

        first = client.post(f"/research/tasks/{task_id}/artifacts/report/promote").json()
        assert first["status"] == "PROMOTED"
        assert first["idempotent"] is False

        second = client.post(f"/research/tasks/{task_id}/artifacts/report/promote").json()
        assert second["idempotent"] is True
        assert second["drive_asset_id"] == first["drive_asset_id"]
        # No second drive write: exactly one report.md outputs asset survives (the task folder
        # itself adds task_spec.json / session_history.json mirrors, so count only the report).
        assert len([a for a in env.drive.assets.rows.values() if a.name == "report.md"]) == 1

    def test_promote_missing_artifact_is_404(self, env):
        client = _make_client(env.drive, env.scratch)
        task_id = client.post("/research/tasks", json={"title": "p"}).json()["task_id"]
        assert client.post(f"/research/tasks/{task_id}/artifacts/nope/promote").status_code == 404


# ── 5. Cascade delete (409-guarded, cloud folder → Trash, scratch removed) ──
class TestDeleteTask:
    async def test_delete_cascades_cloud_folder_and_scratch(self, env):
        client = _make_client(env.drive, env.scratch)
        created = client.post(
            "/research/tasks", json={"title": "doomed", "description": "d"}
        ).json()
        task_id = created["task_id"]
        project = ResearchService._load_json(
            env.scratch / str(USER) / task_id / "project.json", None
        )
        cloud_id = project["cloud_folder_id"]
        assert project["cloud_folder_path"] == "doomed"

        res = client.delete(f"/research/tasks/{task_id}")
        assert res.status_code == 200
        assert res.json() == {"deleted": True}

        # Scratch state is gone; the task is out of the list.
        assert not (env.scratch / str(USER) / task_id).exists()
        assert client.get("/research/tasks").json()["tasks"] == []

        # Cloud task folder soft-deleted: folder row gone, every asset under it trashed.
        assert [f for f in env.drive.folders.rows.values() if f.id == uuid.UUID(cloud_id)] == []
        assert [a for a in env.drive.assets.rows.values() if a.deleted_at is None] == []

    async def test_delete_clears_session_index(self, env):
        svc = ResearchService(env.drive, env.scratch)
        task_id = (await svc.create_task(USER, title="bound"))["task_id"]
        session = uuid.uuid4()
        svc.bind_session(USER, task_id, session)
        assert svc.task_id_for_session(USER, session) == task_id

        client = _make_client(env.drive, env.scratch)
        assert client.delete(f"/research/tasks/{task_id}").status_code == 200
        assert svc.task_id_for_session(USER, session) is None

    async def test_delete_running_task_is_409(self, env):
        client = _make_client(env.drive, env.scratch)
        task_id = client.post("/research/tasks", json={"title": "busy"}).json()["task_id"]
        svc = ResearchService(env.drive, env.scratch)
        svc.record_execution(USER, task_id, tool="research_run.execute_sandbox_script", args={})

        res = client.delete(f"/research/tasks/{task_id}")
        assert res.status_code == 409
        assert "currently running" in res.json()["detail"]
        assert client.get("/research/tasks").json()["tasks"]  # task survives the 409

    async def test_delete_active_run_is_409(self, env):
        client = _make_client(env.drive, env.scratch)
        task_id = client.post("/research/tasks", json={"title": "busy"}).json()["task_id"]
        # A live server-owned run slot (begin_run, the higher-level mutex) blocks delete even
        # with no per-tool execution mid-flight.
        svc = ResearchService(env.drive, env.scratch)
        svc.begin_run(USER, task_id, session_id="sess-1")

        res = client.delete(f"/research/tasks/{task_id}")
        assert res.status_code == 409
        assert "currently running" in res.json()["detail"]
        assert client.get("/research/tasks").json()["tasks"]

    async def test_delete_indexed_report_is_409(self, env):
        client = _make_client(env.drive, env.scratch)
        task_id = client.post("/research/tasks", json={"title": "kb"}).json()["task_id"]
        svc = ResearchService(env.drive, env.scratch)
        await svc.write_scratch(USER, task_id, artifact_id="report", content="# R")
        await svc.promote_to_drive(USER, task_id, artifact_id="report")
        # The RAG worker indexed the outputs asset → deletion is blocked.
        report = next(a for a in env.drive.assets.rows.values() if a.name == "report.md")
        await env.drive.assets.set_status(report.id, rag_status="INDEXED")

        res = client.delete(f"/research/tasks/{task_id}")
        assert res.status_code == 409
        assert "Knowledge Base" in res.json()["detail"]
        assert client.get("/research/tasks").json()["tasks"]

    def test_delete_missing_task_is_404(self, env):
        client = _make_client(env.drive, env.scratch)
        assert client.delete(f"/research/tasks/{uuid.uuid4()}").status_code == 404

    def test_delete_other_users_task_is_404(self, env):
        alice = _make_client(env.drive, env.scratch)
        task_id = alice.post("/research/tasks", json={"title": "A"}).json()["task_id"]
        bob = _make_bob_client(env.drive, env.scratch)
        assert bob.delete(f"/research/tasks/{task_id}").status_code == 404


# ── 6. Backend-restart persistence recovery ─────────────────────────────────
class TestRestartPersistence:
    async def test_fresh_service_recovers_all_state(self, env):
        # First "process": create task + bind a session + mirror turns + artifact + graph.
        svc1 = ResearchService(env.drive, env.scratch)
        task = await svc1.create_task(USER, title="draft task", description="desc")
        task_id = task["task_id"]
        session = uuid.uuid4()
        svc1.bind_session(USER, task_id, session)
        await svc1.append_session_turn(USER, session, "user", "hello")
        await svc1.append_session_turn(USER, session, "assistant", "hi there")
        await svc1.write_scratch(USER, task_id, artifact_id="draft", content="# Draft")
        svc1.record_node(USER, task_id, node={"id": "S", "type": "Source", "label": "src"})

        # Second "process": a brand-new service over the same scratch root.
        svc2 = ResearchService(env.drive, env.scratch)
        assert [t["task_id"] for t in svc2.list_tasks(USER)] == [task_id]
        status = await svc2.get_task_status(USER, task_id)
        assert status["stage"] == "DISCOVER"
        assert status["description"] == "desc"
        assert status["nodes"]["Source"][0]["label"] == "src"
        assert svc2.task_id_for_session(USER, session) == task_id
        mirror = ResearchService._load_json(
            env.scratch / str(USER) / task_id / "session_history.json", None
        )
        assert [t["content"] for t in mirror["turns"]] == ["hello", "hi there"]
        art = svc2.list_artifacts(USER, task_id)
        assert art[0]["artifact_id"] == "draft"
        assert svc2.read_artifact(USER, task_id, artifact_id="draft")["content"] == "# Draft"

    async def test_create_task_is_idempotent_across_restart(self, env):
        svc1 = ResearchService(env.drive, env.scratch)
        first = await svc1.create_task(USER, title="t", idempotency_key="task-k1")

        svc2 = ResearchService(env.drive, env.scratch)
        second = await svc2.create_task(USER, title="t", idempotency_key="task-k1")
        assert second["task_id"] == first["task_id"]
        assert second["idempotent"] is True
        assert len(svc2.list_tasks(USER)) == 1


# ── 7. Research-session isolation (hidden from the chat sidebar list) ───────
class TestSessionIsolation:
    async def test_get_sessions_filters_research_bound_sessions(self, env, monkeypatch):
        from api.routers import sessions as sessions_module

        # A normal chat session plus a session bound to a research task.
        normal_id = str(uuid.uuid4())
        research_session_id = str(uuid.uuid4())
        svc = ResearchService(env.drive, env.scratch)
        task_id = (await svc.create_task(USER, title="t"))["task_id"]
        svc.bind_session(USER, task_id, research_session_id)
        assert svc.bound_session_ids(USER) == {research_session_id}

        async def _fake_list_sessions(*_args, **_kwargs):
            return [
                {"id": normal_id, "created_at": None, "summary": None, "title": "normal"},
                {"id": research_session_id, "created_at": None, "summary": None, "title": "research"},
            ]

        # Point the router at the throwaway scratch root, then exercise the endpoint directly.
        monkeypatch.setattr(sessions_module, "settings", SimpleNamespace(research_scratch_dir=env.scratch))
        monkeypatch.setattr(sessions_module, "list_sessions", _fake_list_sessions)
        result = await sessions_module.get_sessions(user=_auth(USER), q=None, drive=env.drive)
        # The research-bound session is a different kind (tracked in the Research monitor),
        # so the chat sidebar only sees the normal conversation.
        assert [s["id"] for s in result["sessions"]] == [normal_id]
