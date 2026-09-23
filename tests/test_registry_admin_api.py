"""Step-4 admin-plane tests: gate, status mapping, and the audit trail.

A bare app mounting ONLY the registry router keeps these hermetic; the store /
publish functions are patched at the router module's globals (the import sites),
so no DB and no TEI is touched. What must hold regardless: every non-admin
request is refused before any handler runs, every mutation carries the admin
username into registry_audit, and rejected publishes are audited too (they leave
no version row — the audit line is the only trace).
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from api.auth import AuthAdmin, require_admin
from api.routers import registry_admin as ra
from core.application.chat.intent_funnel.registry import (
    CapabilityEntry,
    PublishRejectedError,
    RegistryConflictError,
    RegistryNotFoundError,
    RegistryStateError,
    RegistryVersionView,
)
from core.application.chat.qir.types import Snapshot
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _entry(cid="cap-a", row_version=0):
    return CapabilityEntry(
        capability_id=cid, tool_binding="create_folder", description="d",
        patterns=("新建文件夹",), examples=("e",), row_version=row_version,
    )


def _view(version=3, state="active"):
    return RegistryVersionView(
        version=version, state=state, fingerprint="reg1-x",
        capabilities=(), entries=(_entry(),),
    )


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(ra.router)
    app.dependency_overrides[require_admin] = lambda: AuthAdmin(
        username="root", token_id=uuid4(),
    )
    return TestClient(app)


@pytest.fixture()
def audits(monkeypatch):
    captured: list[dict] = []

    async def fake_audit(action, **kw):
        # mirror store.audit's defaults so assertions see the persisted shape
        kw.setdefault("ok", True)
        kw.setdefault("detail", {})
        captured.append({"action": action, **kw})

    monkeypatch.setattr(ra, "audit", fake_audit)
    return captured


# ── the gate itself ───────────────────────────────────────────────────────────────

def test_unauthenticated_requests_never_reach_a_handler():
    app = FastAPI()
    app.include_router(ra.router)
    anon = TestClient(app)
    for path in ("/admin/registry/drafts", "/admin/registry/versions",
                 "/admin/registry/audit"):
        assert anon.get(path).status_code in (401, 403), path
    assert anon.post("/admin/registry/publish", json={}).status_code in (401, 403)


# ── drafts ────────────────────────────────────────────────────────────────────────

def test_list_drafts_round_trips_row_version(client, monkeypatch):
    async def fake_list(**kw):
        return [_entry(row_version=7)]

    monkeypatch.setattr(ra, "list_drafts", fake_list)
    out = client.get("/admin/registry/drafts").json()["drafts"]
    assert out[0]["capability_id"] == "cap-a" and out[0]["row_version"] == 7


def test_create_draft_audits_the_actor(client, audits, monkeypatch):
    async def fake_create(entry, **kw):
        return entry

    monkeypatch.setattr(ra, "create_draft", fake_create)
    r = client.post("/admin/registry/drafts",
                    json={"capability_id": "cap-a", "tool_binding": "create_folder"})
    assert r.status_code == 200
    a = audits[0]
    assert a["action"] == "draft_create" and a["actor_username"] == "root"
    assert a["target"] == "cap-a" and a["ok"] is True


def test_create_draft_duplicate_is_409(client, audits, monkeypatch):
    async def boom(entry, **kw):
        raise RegistryConflictError("exists")

    monkeypatch.setattr(ra, "create_draft", boom)
    assert client.post("/admin/registry/drafts",
                       json={"capability_id": "cap-a", "tool_binding": "x"}
                       ).status_code == 409
    assert audits == []  # 409 is not a mutation; nothing to record


def test_patch_draft_stale_token_is_409_fresh_is_200(client, audits, monkeypatch):
    async def fake_update(cid, patch, expected, **kw):
        if expected == 0:
            raise RegistryConflictError("changed under you")
        return _entry(cid, row_version=expected + 1)

    monkeypatch.setattr(ra, "update_draft", fake_update)
    body = {"expected_row_version": 0, "patch": {"description": "x"}}
    assert client.patch("/admin/registry/drafts/cap-a", json=body).status_code == 409
    body["expected_row_version"] = 4
    r = client.patch("/admin/registry/drafts/cap-a", json=body)
    assert r.status_code == 200 and r.json()["draft"]["row_version"] == 5
    assert audits[0]["action"] == "draft_update" and audits[0]["ok"] is True
    assert audits[0]["detail"]["patch"] == {"description": "x"}


def test_patch_draft_non_draft_field_is_422(client, monkeypatch):
    async def bad(cid, patch, expected, **kw):
        raise ValueError("non-draft fields in patch")

    monkeypatch.setattr(ra, "update_draft", bad)
    r = client.patch("/admin/registry/drafts/cap-a",
                     json={"expected_row_version": 1, "patch": {"id": "evil"}})
    assert r.status_code == 422


# ── preview / publish / rollback ──────────────────────────────────────────────────

def test_preview_reports_clean_build_without_writes(client, monkeypatch):
    async def fake_list(**kw):
        return [_entry()]

    snap = Snapshot(version="qir1-abc", built_at=0.0, capabilities=())

    async def fake_preview(entries, embedder):
        return [], snap

    monkeypatch.setattr(ra, "list_drafts", fake_list)
    monkeypatch.setattr(ra, "preview_draft", fake_preview)
    monkeypatch.setattr(ra, "_embedder", lambda: object())
    out = client.get("/admin/registry/preview").json()
    assert out == {"issues": [], "capabilities": 1, "snapshot_version": "qir1-abc",
                   "routable": 0}


def test_publish_rejection_is_audited_with_all_issues(client, audits, monkeypatch):
    async def reject(*a, **kw):
        raise PublishRejectedError(["bad binding", "missing examples"])

    monkeypatch.setattr(ra, "publish_draft", reject)
    monkeypatch.setattr(ra, "_embedder", lambda: object())
    r = client.post("/admin/registry/publish", json={"note": "try"})
    assert r.status_code == 422
    assert r.json()["detail"]["issues"] == ["bad binding", "missing examples"]
    assert audits[0]["action"] == "publish_rejected" and audits[0]["ok"] is False
    assert audits[0]["actor_username"] == "root"


def test_publish_success_lands_version_and_audit(client, audits, monkeypatch):
    async def ok(*a, **kw):
        return _view(version=9)

    monkeypatch.setattr(ra, "publish_draft", ok)
    monkeypatch.setattr(ra, "_embedder", lambda: object())
    r = client.post("/admin/registry/publish", json={"note": "go"})
    assert r.status_code == 200 and r.json()["version"] == 9
    assert audits[0]["action"] == "publish" and audits[0]["target"] == "v9"
    assert audits[0]["ok"] is True


def test_publish_state_race_is_409(client, audits, monkeypatch):
    async def race(*a, **kw):
        raise RegistryStateError("concurrent")

    monkeypatch.setattr(ra, "publish_draft", race)
    monkeypatch.setattr(ra, "_embedder", lambda: object())
    assert client.post("/admin/registry/publish", json={}).status_code == 409
    assert audits[0]["ok"] is False


def test_rollback_maps_errors_and_audits_provenance(client, audits, monkeypatch):
    async def ok(version, **kw):
        return _view(version=10)

    async def missing(version, **kw):
        raise RegistryNotFoundError("no v2")

    monkeypatch.setattr(ra, "rollback", ok)
    r = client.post("/admin/registry/versions/10/rollback", json={})
    assert r.status_code == 200
    assert r.json() == {"version": 10, "state": "active", "source_version": 10}
    assert audits[0]["action"] == "rollback"
    monkeypatch.setattr(ra, "rollback", missing)
    assert client.post("/admin/registry/versions/2/rollback", json={}).status_code == 404


def test_versions_listing_and_detail(client, monkeypatch):
    async def fake_versions(**kw):
        return [_view(3), _view(2, state="superseded")]

    async def fake_get(version, **kw):
        if version != 3:
            raise RegistryNotFoundError("gone")
        return _view(3)

    monkeypatch.setattr(ra, "list_versions", fake_versions)
    monkeypatch.setattr(ra, "get_version", fake_get)
    out = client.get("/admin/registry/versions").json()["versions"]
    assert [v["version"] for v in out] == [3, 2]
    assert out[1]["state"] == "superseded"
    assert client.get("/admin/registry/versions/3").status_code == 200
    assert client.get("/admin/registry/versions/1").status_code == 404


def test_audit_read_passthrough(client, monkeypatch):
    async def fake_limit(limit, **kw):
        assert limit <= 500  # cap enforced at the router edge
        return [{"action": "publish", "ok": True}]

    monkeypatch.setattr(ra, "list_audit", fake_limit)
    assert client.get("/admin/registry/audit?limit=9999").json() == {
        "entries": [{"action": "publish", "ok": True}]
    }
