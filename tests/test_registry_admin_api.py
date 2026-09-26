"""Admin-plane tests for the LIVE-table registry console (migration 0014).

A bare app mounting ONLY the registry router keeps these hermetic; the store /
query-plane functions are patched at the router module's import sites, so no DB
and no TEI is touched. What must hold regardless: every non-admin request is
refused before any handler runs; every mutation is gated, history-snapshotted
and audited with the admin username; embedder failures surface as 503 (never a
fake success); rejected writes are 422 with the full issue list and leave no
mutation behind.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from api.auth import AuthAdmin, require_admin
from api.routers import registry_admin as ra
from core.application.chat.intent_funnel import registry as reg
from core.application.chat.intent_funnel.registry import CapabilityEntry
from core.application.chat.intent_funnel.registry.entry import QueryRecord
from core.application.chat.intent_funnel.registry.plugins import DIRECT_TOOLS
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _entry(cid="cap-a", row_version=0, **kw):
    base = {
        "capability_id": cid, "tool_binding": "create_folder", "description": "d",
        "standard_queries": (QueryRecord(id="s1", query="新建文件夹", language="zh"),
                          QueryRecord(id="s2", query="create a folder", language="en")),
        "similar_queries": (QueryRecord(id="m1", query="建个目录", language="zh",
                                     standard_query_id="s1"),),
        "parameters": {"name": {"type": "string", "required": True,
                             "max_len": 120, "description": "folder name"}},
        "row_version": row_version,
    }
    base.update(kw)
    return CapabilityEntry(**base)


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
        kw.setdefault("ok", True)
        kw.setdefault("detail", {})
        captured.append({"action": action, **kw})

    monkeypatch.setattr(ra, "audit", fake_audit)
    return captured


@pytest.fixture()
def writes(monkeypatch):
    """Patch the pre-write machinery (roster, gate, history) to no-ops that
    RECORD their calls; individual store/query fakes are set per test."""
    seen: dict = {"gated": [], "snapshotted": []}

    async def fake_load(**kw):
        return reg.RegistryLiveView(fingerprint="live1-test", entries=())

    async def fake_snapshot(entries, **kw):
        seen["snapshotted"].append([e.capability_id for e in entries])
        return 1

    monkeypatch.setattr(ra, "_roster", lambda: {
        tool: dict(spec.arg_schema) for tool, spec in DIRECT_TOOLS.items()})
    monkeypatch.setattr(ra, "load_live_view", fake_load)
    monkeypatch.setattr(ra, "snapshot_history", fake_snapshot)
    monkeypatch.setattr(ra, "_embedder", lambda: SimpleNamespace(
        embed=lambda texts: [[0.1] for _ in texts]))
    return seen


# ── the gate itself ───────────────────────────────────────────────────────────────

def test_unauthenticated_requests_never_reach_a_handler():
    app = FastAPI()
    app.include_router(ra.router)
    anon = TestClient(app)
    for path in ("/admin/registry/capabilities", "/admin/registry/versions",
                 "/admin/registry/audit", "/admin/registry/validate",
                 "/admin/registry/embedding-status", "/admin/registry/live-view",
                 "/admin/registry/tool-schemas"):
        assert anon.get(path).status_code in (401, 403), path
    assert anon.post("/admin/registry/capabilities",
                     json={"capability_id": "x", "tool_binding": "y"}
                     ).status_code in (401, 403)


# ── capabilities (live row) ───────────────────────────────────────────────────────

def test_list_capabilities_round_trips_row_version(client, monkeypatch):
    async def fake_list(**kw):
        return [_entry(row_version=7)]

    monkeypatch.setattr(ra, "list_capabilities", fake_list)
    out = client.get("/admin/registry/capabilities").json()["capabilities"]
    assert out[0]["capability_id"] == "cap-a" and out[0]["row_version"] == 7
    # the corpus is visible in the entry JSON (it IS the runtime truth)
    assert out[0]["standard_queries"][0]["query"] == "新建文件夹"


def test_create_capability_audits_the_actor(client, audits, writes, monkeypatch):
    async def fake_create(entry, **kw):
        return entry

    monkeypatch.setattr(ra, "create_capability", fake_create)
    r = client.post("/admin/registry/capabilities", json={
        "capability_id": "cap-a", "tool_binding": "create_folder",
        "description": "d",
        "parameters": {"name": {"type": "string", "required": True,
                                "max_len": 120, "description": "n"}},
    })
    assert r.status_code == 200
    a = audits[0]
    assert a["action"] == "capability_create" and a["actor_username"] == "root"
    assert a["target"] == "cap-a" and a["ok"] is True
    # the pre-write content rode the history lane exactly once
    assert writes["snapshotted"] == [[]]


def test_create_gate_closed_is_422_with_no_mutation(client, audits, writes,
                                                    monkeypatch):
    called = {"create": 0}

    async def fake_create(entry, **kw):
        called["create"] += 1
        return entry

    monkeypatch.setattr(ra, "create_capability", fake_create)
    monkeypatch.setattr(ra, "validate_entries", lambda entries, **kw: [
        "cap-x: tool_binding 'ghost' is not a tool in the live ToolRuntime roster"])
    r = client.post("/admin/registry/capabilities",
                    json={"capability_id": "cap-x", "tool_binding": "ghost"})
    assert r.status_code == 422
    assert "roster" in r.json()["detail"]["issues"][0]
    assert called["create"] == 0 and writes["snapshotted"] == []
    assert audits == []  # a gated write never happened: nothing to audit


def test_create_duplicate_is_409(client, audits, writes, monkeypatch):
    async def boom(entry, **kw):
        raise reg.RegistryConflictError("exists")

    monkeypatch.setattr(ra, "create_capability", boom)
    monkeypatch.setattr(ra, "validate_entries", lambda entries, **kw: [])
    assert client.post("/admin/registry/capabilities",
                       json={"capability_id": "cap-a", "tool_binding": "x"}
                       ).status_code == 409
    assert audits == []  # 409 is not a mutation; nothing to record


def test_patch_stale_token_is_409_fresh_is_200(client, audits, writes, monkeypatch):
    async def fake_get(cid, **kw):
        return _entry(cid, row_version=4)

    async def fake_update(cid, patch, expected, **kw):
        if expected == 0:
            raise reg.RegistryConflictError("changed under you")
        return _entry(cid, row_version=expected + 1)

    monkeypatch.setattr(ra, "get_capability", fake_get)
    monkeypatch.setattr(ra, "update_capability", fake_update)
    body = {"expected_row_version": 0, "patch": {"description": "x"}}
    assert client.patch("/admin/registry/capabilities/cap-a",
                        json=body).status_code == 409
    body["expected_row_version"] = 4
    r = client.patch("/admin/registry/capabilities/cap-a", json=body)
    assert r.status_code == 200 and r.json()["capability"]["row_version"] == 5
    assert audits[0]["action"] == "capability_update" and audits[0]["ok"] is True
    assert audits[0]["detail"]["patch"] == {"description": "x"}


def test_patch_non_capability_field_is_422(client, writes, monkeypatch):
    async def fake_get(cid, **kw):
        return _entry(cid)

    monkeypatch.setattr(ra, "get_capability", fake_get)
    r = client.patch("/admin/registry/capabilities/cap-a",
                     json={"expected_row_version": 1,
                           "patch": {"standard_queries": "evil"}})
    assert r.status_code == 422


def test_patch_missing_row_is_404(client, monkeypatch):
    async def none(cid, **kw):
        return None

    monkeypatch.setattr(ra, "get_capability", none)
    r = client.patch("/admin/registry/capabilities/cap-gone",
                     json={"expected_row_version": 0, "patch": {"description": "x"}})
    assert r.status_code == 404


# ── query plane (embed-then-write) ────────────────────────────────────────────────

def test_add_standard_embedder_failure_is_503(client, audits, writes, monkeypatch):
    async def fake_get(cid, **kw):
        return _entry(cid)

    async def boom(capability_id, text, **kw):
        raise reg.RegistryError("tei down: write aborted")

    monkeypatch.setattr(ra, "get_capability", fake_get)
    monkeypatch.setattr(ra.queries, "add_standard_query", boom)
    r = client.post("/admin/registry/capabilities/cap-a/queries/standard",
                    json={"query": "another folder"})
    assert r.status_code == 503
    assert audits == []  # failed writes are honest, not audited as successes


def test_add_standard_success_audits_and_returns_row(client, audits, writes,
                                                     monkeypatch):
    async def fake_get(cid, **kw):
        return _entry(cid)

    async def ok(capability_id, text, **kw):
        return {"id": "9", "query": text, "language": "en"}

    monkeypatch.setattr(ra, "get_capability", fake_get)
    monkeypatch.setattr(ra.queries, "add_standard_query", ok)
    # the default entry already carries zh+en standards; a THIRD language-free
    # sentence derives en -> the gate would see two en rows, so gate passes
    # only because the canned _roster/gate here is the real validator on the
    # projected entry: use validate monkeypatch to isolate the router contract
    monkeypatch.setattr(ra, "validate_entries", lambda entries, **kw: [])
    r = client.post("/admin/registry/capabilities/cap-a/queries/standard",
                    json={"query": "create folder please"})
    assert r.status_code == 200 and r.json()["query"]["language"] == "en"
    assert audits[0]["action"] == "query_add"
    assert audits[0]["detail"] == {"table": "standard", "id": "9", "language": "en"}


def test_add_similar_duplicate_language_is_409(client, writes, monkeypatch):
    async def fake_get(cid, **kw):
        return _entry(cid)

    async def conflict(standard_query_id, text, **kw):
        raise reg.RegistryConflictError("language taken")

    monkeypatch.setattr(ra, "get_capability", fake_get)
    monkeypatch.setattr(ra, "validate_entries", lambda entries, **kw: [])
    monkeypatch.setattr(ra.queries, "add_similar_query", conflict)
    r = client.post("/admin/registry/capabilities/cap-a/queries/similar",
                    json={"standard_query_id": "s1", "query": "建个目录吧"})
    assert r.status_code == 409


def test_query_text_patch_maps_errors(client, writes, monkeypatch):
    async def fake_list(**kw):
        return [_entry()]

    async def not_found(table, query_id, text, **kw):
        raise reg.RegistryNotFoundError("gone")

    monkeypatch.setattr(ra, "list_capabilities", fake_list)
    monkeypatch.setattr(ra.queries, "update_query_text", not_found)
    # unknown query id -> 404 from the owner lookup itself
    assert client.patch("/admin/registry/queries/standard/nope",
                        json={"query": "x"}).status_code == 404
    # negatives are not match data: text edits refused at the router edge
    assert client.patch("/admin/registry/queries/negative/s1",
                        json={"query": "x"}).status_code == 422
    # existing row but the embedder write fails -> 503
    monkeypatch.setattr(ra, "validate_entries", lambda entries, **kw: [])
    async def boom(table, query_id, text, **kw):
        raise reg.RegistryError("tei down")

    monkeypatch.setattr(ra.queries, "update_query_text", boom)
    assert client.patch("/admin/registry/queries/standard/s1",
                        json={"query": "新建文件夹"}).status_code == 503


def test_negative_toggle_skips_gate_and_projection(client, audits, writes,
                                                   monkeypatch):
    snapshot_calls = writes

    async def ok(table, query_id, enabled, **kw):
        return {"id": query_id, "enabled": enabled}

    monkeypatch.setattr(ra.queries, "set_query_enabled", ok)
    r = client.post("/admin/registry/queries/negative/abc/enabled",
                    json={"enabled": False})
    assert r.status_code == 200 and r.json()["query"]["enabled"] is False
    # negatives changed NO routable content: still snapshotted (history of the
    # write itself) and audited, but the entry projection never mentioned them
    assert snapshot_calls["snapshotted"] == [[]]
    assert audits[0]["action"] == "query_toggle"


def test_delete_cascade_flag_passthrough(client, writes, monkeypatch):
    async def fake_list(**kw):
        return [_entry()]

    seen = {}

    async def ok(table, query_id, *, cascade_similar=False, **kw):
        seen["cascade"] = cascade_similar
        return {"deleted": query_id, "table": table}

    monkeypatch.setattr(ra, "list_capabilities", fake_list)
    monkeypatch.setattr(ra, "validate_entries", lambda entries, **kw: [])
    monkeypatch.setattr(ra.queries, "delete_query", ok)
    r = client.delete("/admin/registry/queries/standard/s1?cascade_similar=true")
    assert r.status_code == 200 and seen["cascade"] is True


# ── honest reporting endpoints ─────────────────────────────────────────────────────

def test_embedding_status_includes_pending_count(client, monkeypatch):
    async def fake_status(**kw):
        return {"standard": {"total": 36, "embedded": 36},
                "similar": {"total": 514, "embedded": 500},
                "embedding_profile": {"model": "BAAI/bge-m3", "dim": 1024}}

    async def pending(**kw):
        return [{"table": "similar", "id": str(i), "query": "q"} for i in range(14)]

    monkeypatch.setattr(ra, "embedding_status", fake_status)
    monkeypatch.setattr(ra.queries, "pending_embeddings", pending)
    out = client.get("/admin/registry/embedding-status").json()
    assert out["pending"] == 14
    assert out["embedding_profile"]["model"] == "BAAI/bge-m3"


def test_validate_endpoint_is_zero_write(client, monkeypatch):
    async def fake_list(**kw):
        return [_entry()]

    monkeypatch.setattr(ra, "list_capabilities", fake_list)
    monkeypatch.setattr(ra, "_roster", lambda: {"create_folder": {"name": 120}})
    out = client.get("/admin/registry/validate").json()
    assert out["capabilities"] == 1 and out["issues"] == []


def test_live_view_reports_fingerprint_and_routability(client, monkeypatch):
    async def fake_active(**kw):
        return reg.RegistryLiveView(
            fingerprint="live1-abc", entries=(_entry(),
                                              _entry("cap-off", enabled=False,
                                                     status="disabled")))

    monkeypatch.setattr(ra, "active_view", fake_active)
    out = client.get("/admin/registry/live-view").json()
    assert out["fingerprint"] == "live1-abc"
    assert {c["capability_id"]: c["routable"] for c in out["capabilities"]} == {
        "cap-a": True, "cap-off": False}


def test_live_view_none_when_tables_empty(client, monkeypatch):
    async def none(**kw):
        return None

    monkeypatch.setattr(ra, "active_view", none)
    out = client.get("/admin/registry/live-view").json()
    assert out["fingerprint"] is None and out["capabilities"] == []


def test_tool_schemas_projection_is_the_pure_roster(client, monkeypatch):
    from api import deps

    fake_schema = {"name": "create_folder", "description": "d",
                   "parameters": {"type": "object", "properties": {
                       "name": {"type": "string", "maxLength": 120}}}}
    monkeypatch.setattr(
        deps, "get_agent_kernel",
        lambda: SimpleNamespace(runtime=SimpleNamespace(
            schemas=lambda: [fake_schema])))
    out = client.get("/admin/registry/tool-schemas").json()["tools"]
    # pure ToolRuntime.schemas() projection (ruling 2026-09-26): the legacy
    # DIRECT_TOOLS cross-check flag is retired — the roster is the truth.
    assert out[0]["name"] == "create_folder"
    assert "in_direct_tools" not in out[0]


# ── catalog (inventory -> capability row admission) ───────────────────────────────

def test_catalog_view_joins_capabilities_by_tool(client, monkeypatch):
    async def fake_catalog(**kw):
        return [{"action_key": "create-folder", "display_name": "Create Folder",
                 "description": "d", "tool_binding": "create_folder",
                 "route": "agent", "implementation_ref": "r", "status": "user_facing"},
                {"action_key": "other", "display_name": "Other",
                 "description": "d", "tool_binding": "not_a_tool",
                 "route": "agent", "implementation_ref": "r", "status": "user_facing"}]

    async def fake_caps(**kw):
        return [_entry()]

    monkeypatch.setattr(ra, "list_catalog", fake_catalog)
    monkeypatch.setattr(ra, "list_capabilities", fake_caps)
    acts = client.get("/admin/registry/catalog").json()["actions"]
    by = {a["action_key"]: a for a in acts}
    assert by["create-folder"]["registered"] is True
    assert by["create-folder"]["capability_id"] == "cap-a"
    assert by["create-folder"]["registry_corpus"]["standard"] == 2
    assert by["other"]["registered"] is False and by["other"]["bindable"] is False


def test_catalog_create_is_born_disabled(client, audits, writes, monkeypatch):
    async def fake_catalog(action_key, **kw):
        return {"action_key": action_key, "display_name": "Create Folder",
                "description": "makes folders", "tool_binding": "create_folder"}

    async def none(cid, **kw):
        return None

    async def fake_create(entry, **kw):
        captured["entry"] = entry
        return entry

    captured: dict = {}
    monkeypatch.setattr(ra, "get_catalog", fake_catalog)
    monkeypatch.setattr(ra, "get_capability", none)
    monkeypatch.setattr(ra, "create_capability", fake_create)
    r = client.post("/admin/registry/catalog/create-folder/create", json={})
    assert r.status_code == 200
    e = captured["entry"]
    assert e.capability_id == "cap-create-folder"
    assert e.enabled is False and e.status == "disabled"
    # parameters default to the mechanical ToolRuntime roster projection (the
    # 120 bound lives on the runtime schema; it happens to equal the legacy
    # L0 spec, which is now only a value reference, never the truth)
    assert e.parameters["name"]["max_len"] == DIRECT_TOOLS["create_folder"].arg_schema["name"]
    assert audits[0]["action"] == "catalog_create"


def test_catalog_create_refuses_non_executable_binding(client, writes, monkeypatch):
    async def fake_catalog(action_key, **kw):
        return {"action_key": action_key, "display_name": "X",
                "description": "d", "tool_binding": "not_a_tool"}

    monkeypatch.setattr(ra, "get_catalog", fake_catalog)
    r = client.post("/admin/registry/catalog/x/create", json={})
    assert r.status_code == 409
    assert "ToolRuntime" in r.json()["detail"]


# ── history + rollback (restore + re-embed) ───────────────────────────────────────

def test_versions_listing_and_detail(client, monkeypatch):
    async def fake_versions(**kw):
        return [{"version": 3, "state": "historical"},
                {"version": 2, "state": "historical"}]

    async def fake_get(version, **kw):
        if version != 3:
            raise reg.RegistryNotFoundError("gone")
        return {"version": 3, "state": "historical"}

    monkeypatch.setattr(ra, "list_versions", fake_versions)
    monkeypatch.setattr(ra, "get_version", fake_get)
    out = client.get("/admin/registry/versions").json()["versions"]
    assert [v["version"] for v in out] == [3, 2]
    assert client.get("/admin/registry/versions/3").status_code == 200
    assert client.get("/admin/registry/versions/1").status_code == 404


def test_rollback_missing_version_is_404(client, audits, monkeypatch):
    async def missing(version, **kw):
        raise reg.RegistryNotFoundError("no v2")

    monkeypatch.setattr(ra, "version_entries", missing)
    assert client.post("/admin/registry/versions/2/rollback",
                       json={}).status_code == 404


def test_rollback_replays_snapshot_and_audits(client, audits, writes, monkeypatch):
    target = _entry("cap-a")

    async def fake_targets(version, **kw):
        return [target]

    async def fake_get(cid, **kw):
        return target

    async def fake_update(cid, patch, expected, **kw):
        return target

    async def fake_list(**kw):
        return [target]

    q = ra.queries
    monkeypatch.setattr(ra, "version_entries", fake_targets)
    monkeypatch.setattr(ra, "get_capability", fake_get)
    monkeypatch.setattr(ra, "update_capability", fake_update)
    monkeypatch.setattr(ra, "list_capabilities", fake_list)
    monkeypatch.setattr(ra, "validate_entries", lambda entries, **kw: [])
    monkeypatch.setattr(q, "list_queries", _empty_queries)
    monkeypatch.setattr(q, "add_standard_query", _added("std"))
    monkeypatch.setattr(q, "add_similar_query", _added("sim"))
    monkeypatch.setattr(q, "add_negative_query", _added("neg"))
    r = client.post("/admin/registry/versions/5/rollback", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["restored_version"] == 5 and body["capabilities"] == 1
    assert body["issues"] == []
    a = [x for x in audits if x["action"] == "rollback"][-1]
    assert a["ok"] is True and a["target"] == "v5"


def test_rollback_embedder_failure_is_honest_503(client, audits, writes, monkeypatch):
    target = _entry("cap-a")

    async def fake_targets(version, **kw):
        return [target]

    async def fake_get(cid, **kw):
        return target

    q = ra.queries
    monkeypatch.setattr(ra, "version_entries", fake_targets)
    monkeypatch.setattr(ra, "get_capability", fake_get)
    monkeypatch.setattr(q, "list_queries", _empty_queries)

    async def boom(capability_id, text, **kw):
        raise reg.RegistryError("tei down mid-restore")

    monkeypatch.setattr(q, "add_standard_query", boom)
    r = client.post("/admin/registry/versions/5/rollback", json={})
    assert r.status_code == 503 and "partial restore" in r.json()["detail"]
    failed = [x for x in audits if x["action"] == "rollback"][-1]
    assert failed["ok"] is False  # the partial restore is audited as a failure


async def _empty_queries(capability_id, **kw):
    return {"capability_id": capability_id, "standard": [], "similar": [],
            "negative": []}


def _added(tag):
    async def f(*a, **kw):
        return {"id": f"{tag}-1", "query": str(a[1] if len(a) > 1 else ""),
                "language": "zh"}
    return f


# ── §8.5 preview-route passthrough (the dry-run box on the History tab) ──────────

def test_preview_route_strips_query_and_passthrough_verdict(client, monkeypatch):
    from core.application.chat.intent_funnel import funnel

    seen = {}

    async def fake_preview(query, *, deps):
        seen["query"] = query
        seen["deps_embedder"] = callable(deps.embedder)  # CALLABLE, not instance
        return {"final_route": "agent", "fallback_reason": "TOOL_INTENT_REJECT",
                "candidates": [], "execution_mode": "preview"}

    monkeypatch.setattr(funnel, "preview", fake_preview)
    r = client.post("/admin/registry/preview-route", json={"query": "  新建文件夹?  "})
    assert r.status_code == 200
    assert seen["query"] == "新建文件夹?"
    assert seen["deps_embedder"] is True
    assert r.json()["execution_mode"] == "preview"
    # the schema gate: an empty query never reaches the funnel
    assert client.post("/admin/registry/preview-route",
                       json={"query": ""}).status_code == 422


# ── audit read ────────────────────────────────────────────────────────────────────

def test_audit_read_passthrough(client, monkeypatch):
    async def fake_limit(*, limit, **kw):
        assert limit <= 500  # cap enforced at the router edge
        return [{"action": "capability_update", "ok": True}]

    monkeypatch.setattr(ra, "list_audit", fake_limit)
    assert client.get("/admin/registry/audit?limit=9999").json() == {
        "entries": [{"action": "capability_update", "ok": True}]
    }
