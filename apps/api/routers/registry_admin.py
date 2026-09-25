"""Intent Registry console (admin): drafts CRUD, validate/preview, publish,
version history and rollback (QIR P1 step 4).

Permission posture (P1 ruling 5, ratified): NO new RBAC framework — every route
here rides the existing console admin gate (``require_admin``), and the admin
username is recorded as the audit actor on every mutation. Fine-grained
EDIT/VALIDATE/PUBLISH/ROLLBACK separation is deferred; §8.18's hard constraint
is met: ordinary users cannot reach this router at all, and Publish/Rollback
(plus rejected attempts, which leave no version row) are audit-recorded.
"""
from __future__ import annotations

from api.auth import AuthAdmin, require_admin
from api.deps import _embedder
from api.schemas import (
    RegistryDraftCreateRequest,
    RegistryDraftUpdateRequest,
    RegistryDraftParamsRequest,
    RegistryPreviewRouteRequest,
    RegistryPublishRequest,
    RegistryQueryDraftRequest,
    RegistryRollbackRequest,
)
from core.application.chat.intent_funnel.registry import (
    CapabilityEntry,
    PublishRejectedError,
    RegistryConflictError,
    RegistryError,
    RegistryNotFoundError,
    RegistryStateError,
    active_view,
    audit,
    create_draft,
    get_version,
    list_audit,
    list_drafts,
    list_versions,
    preview_draft,
    publish_draft,
    rollback,
    update_draft,
)
from core.application.chat.intent_funnel.registry import catalog
from core.application.chat.intent_funnel.registry.plugins import DIRECT_TOOLS
from core.infrastructure.db import SessionLocal
from core.infrastructure.request_context import (
    reset_request_execution_mode,
    set_request_execution_mode,
)
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(tags=["registry-admin"])


def _entry_json(e: CapabilityEntry) -> dict:
    return {**e.to_payload(), "row_version": e.row_version}


# ── drafts (editable source of truth) ─────────────────────────────────────────────

@router.get("/admin/registry/drafts")
async def get_drafts(_: AuthAdmin = Depends(require_admin)) -> dict:
    return {"drafts": [_entry_json(e) for e in await list_drafts(session_factory=SessionLocal)]}


@router.post("/admin/registry/drafts")
async def post_draft(
    body: RegistryDraftCreateRequest, admin: AuthAdmin = Depends(require_admin)
) -> dict:
    entry = CapabilityEntry(
        capability_id=body.capability_id.strip(),
        tool_binding=body.tool_binding.strip(),
        description=body.description,
        patterns=tuple(body.patterns), aliases=tuple(body.aliases),
        examples=tuple(body.examples), negatives=tuple(body.negatives),
        standard_example=body.standard_example,
        synonym_examples=tuple(body.synonym_examples),
        parameters=dict(body.parameters),
        arg_slots=dict(body.arg_slots), permissions=body.permissions,
        execution_policy=body.execution_policy, intent_kind=body.intent_kind,
        enabled=body.enabled,
        status=body.status, replacement_capability_id=body.replacement_capability_id,
    )
    try:
        created = await create_draft(entry, session_factory=SessionLocal)
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await audit("draft_create", actor_username=admin.username,
                target=body.capability_id, session_factory=SessionLocal)
    return {"draft": _entry_json(created)}


@router.patch("/admin/registry/drafts/{capability_id}")
async def patch_draft(
    capability_id: str, body: RegistryDraftUpdateRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    try:
        updated = await update_draft(
            capability_id, body.patch, body.expected_row_version,
            session_factory=SessionLocal,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # non-draft field in patch
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit("draft_update", actor_username=admin.username, target=capability_id,
                detail={"patch": body.patch, "row_version": updated.row_version},
                session_factory=SessionLocal)
    return {"draft": _entry_json(updated)}


# ── Tool Schema (runtime truth, read-only) ────────────────────────────────────────
# The Advanced tab renders PARAMETERS from the SAME schema the Agent loop hands the
# model (``ToolRuntime.schemas()`` -> ``ToolDefinition.parameters`` JSON Schema).
# This endpoint is a projection, not a second parameter model: it writes nothing,
# and a tool that is absent here has no runtime binding (Registry inadmissibility
# is derived separately from DIRECT_TOOLS — never inferred from this listing).

@router.get("/admin/registry/tool-schemas")
async def get_tool_schemas(_: AuthAdmin = Depends(require_admin)) -> dict:
    from api.deps import get_agent_kernel

    schemas = get_agent_kernel().runtime.schemas()
    return {"tools": [
        {
            "name": s["name"],
            "description": s.get("description", ""),
            "parameters": s.get("parameters", {}),
            "in_direct_tools": s["name"] in DIRECT_TOOLS,
        }
        for s in sorted(schemas, key=lambda x: x["name"])
    ]}


# ── Action Catalog (Action Universe, migration 0011) ─────────────────────────────
# Inventory + PRE-REGISTRY query drafts. These routes can never publish or write
# the runtime corpus: register copies a Draft Configuration into a capabilities
# DRAFT row, and from there the ONLY way to active is the existing
# Draft -> Validate -> Publish lifecycle below.

async def _catalog_view() -> dict:
    """Catalog rows joined to the Registry: registered / published / funnel
    eligibility computed at read time (no stored second truth)."""
    rows = await catalog.list_catalog(session_factory=SessionLocal)
    drafts = await list_drafts(session_factory=SessionLocal)
    by_tool = {d.tool_binding: d for d in drafts}
    view = await active_view(session_factory=SessionLocal)
    published = {
        e.capability_id for e in (view.entries if view is not None else ())
        if e.enabled and e.status == "active"
    }
    counts = await catalog.draft_counts(SessionLocal)
    actions = []
    for r in rows:
        d = by_tool.get(r["tool_binding"])
        funnel = "not configured"
        route = r["route"]
        if d is not None:
            routable = d.enabled and d.status == "active" and d.intent_kind == "action"
            funnel = "enabled" if (routable and d.capability_id in published) else (
                "disabled" if not d.enabled or d.status != "active" else "draft (not published)")
            route = "funnel" if funnel == "enabled" else "agent"
        actions.append({
            **r,
            "bindable": r["tool_binding"] in DIRECT_TOOLS,
            "registered": d is not None,
            "capability_id": d.capability_id if d else None,
            "registry_status": d.status if d else None,
            "registry_enabled": d.enabled if d else None,
            "intent_kind": d.intent_kind if d else None,
            "published": bool(d and d.capability_id in published),
            "funnel": funnel,
            "current_route": route,
            "registry_corpus": (
                {
                    "standard": 1 if (d.standard_example or "").strip() else 0,
                    "similar": len([s for s in d.synonym_examples if str(s).strip()]),
                    "negatives": len([s for s in d.negatives if str(s).strip()]),
                } if d is not None else None
            ),
            "query_draft": counts.get(r["action_key"]),
        })
    # Draft rows whose binding has no catalog row (added via the raw drafts API)
    # must still show up — the Universe never silently loses a routable action.
    catalog_tools = {r["tool_binding"] for r in rows}
    for d in drafts:
        if d.tool_binding in catalog_tools:
            continue
        actions.append({
            "action_key": d.capability_id, "display_name": d.capability_id,
            "description": d.description, "tool_binding": d.tool_binding,
            "route": "agent", "implementation_ref": "(no catalog row — drafts API only)",
            "status": "user_facing",
            "bindable": d.tool_binding in DIRECT_TOOLS,
            "registered": True, "capability_id": d.capability_id,
            "registry_status": d.status, "registry_enabled": d.enabled,
            "intent_kind": d.intent_kind,
            "published": d.capability_id in published,
            "funnel": "draft (not published)", "current_route": "agent",
            "registry_corpus": {
                "standard": 1 if (d.standard_example or "").strip() else 0,
                "similar": len([s for s in d.synonym_examples if str(s).strip()]),
                "negatives": len([s for s in d.negatives if str(s).strip()]),
            },
            "query_draft": None,
        })
    return {"actions": actions}


@router.get("/admin/registry/catalog")
async def get_catalog(_: AuthAdmin = Depends(require_admin)) -> dict:
    return await _catalog_view()


@router.get("/admin/registry/catalog/{action_key}/query-draft")
async def get_catalog_query_draft(
    action_key: str, _: AuthAdmin = Depends(require_admin)
) -> dict:
    cat = await catalog.get_catalog(action_key, session_factory=SessionLocal)
    if cat is None:
        raise HTTPException(status_code=404, detail=f"unknown action {action_key!r}")
    draft = await catalog.get_query_draft(action_key, session_factory=SessionLocal)
    drafts = await list_drafts(session_factory=SessionLocal)
    draft["registered"] = any(d.tool_binding == cat["tool_binding"] for d in drafts)
    return draft


@router.put("/admin/registry/catalog/{action_key}/query-draft")
async def put_catalog_query_draft(
    action_key: str, body: RegistryQueryDraftRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    """Save the PRE-REGISTRY Draft Configuration. Registered actions are
    rejected: one editor per sentence, their corpus lives in the capabilities
    Draft row (Queries tab) — no second lifecycle, no silent divergence."""
    drafts = await list_drafts(session_factory=SessionLocal)
    rows = await catalog.list_catalog(session_factory=SessionLocal)
    me = next((r for r in rows if r["action_key"] == action_key), None)
    if me is None:
        raise HTTPException(status_code=404, detail=f"unknown action {action_key!r}")
    if any(d.tool_binding == me["tool_binding"] for d in drafts):
        raise HTTPException(
            status_code=409,
            detail=f"{action_key} is registered — edit its Query corpus on the "
                   "Registry draft (Queries tab), not as a catalog draft",
        )
    try:
        saved = await catalog.upsert_query_draft(
            action_key,
            standard=body.standard_example,
            similar=body.synonym_examples,
            negatives=body.negatives,
            updated_by=admin.username,
            session_factory=SessionLocal,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit("query_draft_update", actor_username=admin.username, target=action_key,
                detail={"similar": len(saved["similar"]), "negatives": len(saved["negatives"])},
                session_factory=SessionLocal)
    saved["registered"] = False
    return saved


@router.put("/admin/registry/catalog/{action_key}/params-draft")
async def put_catalog_params_draft(
    action_key: str, body: RegistryDraftParamsRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    """Save the PRE-REGISTRY parameter configuration (migration 0012). Same
    shapes as the Registry capability fields — register copies them 1:1. The
    runtime TOOL SCHEMA is never stored here; it is read live via
    GET /admin/registry/tool-schemas. Registered actions are rejected: their
    parameters live in the capabilities Draft row (one editor per field)."""
    drafts = await list_drafts(session_factory=SessionLocal)
    rows = await catalog.list_catalog(session_factory=SessionLocal)
    me = next((r for r in rows if r["action_key"] == action_key), None)
    if me is None:
        raise HTTPException(status_code=404, detail=f"unknown action {action_key!r}")
    if any(d.tool_binding == me["tool_binding"] for d in drafts):
        raise HTTPException(
            status_code=409,
            detail=f"{action_key} is registered — edit its parameters on the "
                   "Registry draft (Advanced tab), not as a catalog draft",
        )
    try:
        saved = await catalog.upsert_draft_parameters(
            action_key,
            parameters=body.parameters, arg_slots=body.arg_slots,
            updated_by=admin.username,
            session_factory=SessionLocal,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit("params_draft_update", actor_username=admin.username, target=action_key,
                detail={"parameters": len(saved["parameters"]),
                        "arg_slots": len(saved["arg_slots"])},
                session_factory=SessionLocal)
    saved["registered"] = False
    return saved


@router.post("/admin/registry/catalog/{action_key}/register")
async def post_catalog_register(
    action_key: str, admin: AuthAdmin = Depends(require_admin)
) -> dict:
    """Agent-only -> Registry DRAFT (the Enable-in-Intent-Funnel admission).
    Copies the Draft Configuration into a new capabilities row; the action then
    walks the existing Draft -> Validate -> Publish path like any other. This
    endpoint never publishes and never touches the active index."""
    rows = await catalog.list_catalog(session_factory=SessionLocal)
    me = next((r for r in rows if r["action_key"] == action_key), None)
    if me is None:
        raise HTTPException(status_code=404, detail=f"unknown action {action_key!r}")
    drafts = await list_drafts(session_factory=SessionLocal)
    if any(d.tool_binding == me["tool_binding"] for d in drafts):
        raise HTTPException(status_code=409, detail=f"{action_key} is already registered")
    spec = DIRECT_TOOLS.get(me["tool_binding"])
    if spec is None:
        # Honest refusal: the publish gate rejects non-DIRECT_TOOLS bindings,
        # and with no hard-delete a premature row would block EVERY publish.
        # Admitting a tool to DIRECT_TOOLS is a separate RUNTIME task.
        raise HTTPException(
            status_code=409,
            detail=f"tool {me['tool_binding']!r} is not an executable DIRECT_TOOLS "
                   "binding yet — Registry cannot invent executables. Runtime "
                   "admission of this tool is a separate task.",
        )
    draft = await catalog.get_query_draft(action_key, session_factory=SessionLocal)
    if not draft["standard"]:
        raise HTTPException(
            status_code=422,
            detail="configure a Standard Query before registering "
                   "(publish gate requires a non-empty intent corpus)",
        )
    capability_id = f"cap-{action_key}"
    # Parameters: the admin's pre-Registry configuration (Advanced tab) wins;
    # with none saved, fall back to the mechanical spec.arg_schema projection
    # so registration never produces a parameter-less action.
    parameters = dict(draft.get("parameters") or {})
    if not parameters:
        parameters = {
            slot: {
                "type": "string",
                "description": f"{slot} argument of {me['tool_binding']}",
                "required": True,
                "max_len": int(bound),
            }
            for slot, bound in (spec.arg_schema or {}).items()
        }
    entry = CapabilityEntry(
        capability_id=capability_id,
        tool_binding=me["tool_binding"],
        description=me["description"] or me["display_name"],
        standard_example=draft["standard"],
        synonym_examples=tuple(draft["similar"]),
        negatives=tuple(draft["negatives"]),
        parameters=parameters,
        arg_slots=dict(draft.get("arg_slots") or {}),
        intent_kind="action",
        enabled=True,
        status="active",
    )
    try:
        created = await create_draft(entry, session_factory=SessionLocal)
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await audit("catalog_register", actor_username=admin.username,
                target=capability_id,
                detail={"action_key": action_key, "from": "action_catalog"},
                session_factory=SessionLocal)
    return {"draft": _entry_json(created)}


# ── validate / preview / publish / rollback ───────────────────────────────────────

@router.get("/admin/registry/intent-corpus/{capability_id}")
async def get_intent_corpus(
    capability_id: str, _: AuthAdmin = Depends(require_admin),
) -> dict:
    """Phase 4 read-only view: WHICH sentences of this draft row feed the Exact
    Match set and the vector library (qir_examples, migration 0009), and which
    of them the CURRENTLY ACTIVE index actually carries. Draft ≠ active: a
    freshly edited sentence shows in_library=false until a publish lands it."""
    from core.application.chat.qir import store as qir_store

    drafts = await list_drafts(session_factory=SessionLocal)
    entry = next(
        (e for e in drafts if e.capability_id == capability_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no draft for {capability_id!r}")
    snap = await qir_store.active(SessionLocal)
    cap = snap.get(capability_id) if snap is not None else None
    active_sentences = set(cap.examples) if cap is not None else set()
    return {
        "capability_id": capability_id,
        "qir_version": snap.version if snap is not None else None,
        "sentences": [
            {"text": s,
             "kind": "canonical" if i == 0 else "synonym",
             "in_active_index": s in active_sentences}
            for i, s in enumerate(entry.intent_corpus)
        ],
        "card_only": {
            "legacy_examples": list(entry.examples),
            "negatives": list(entry.negatives),
        },
        "legacy_inert": {"patterns": list(entry.patterns),
                         "aliases": list(entry.aliases)},
    }


@router.get("/admin/registry/preview")
async def get_preview(_: AuthAdmin = Depends(require_admin)) -> dict:
    """Dry-run the publish BUILD (validation + real embeddings) with zero writes.
    8.5's full-funnel query preview rides on top once step 5 marks execution_mode."""
    drafts = await list_drafts(session_factory=SessionLocal)
    # 8.14: the build's embedding calls are preview usage, not anyone's bill.
    token = set_request_execution_mode("preview")
    try:
        issues, snap = await preview_draft(drafts, _embedder())
    finally:
        reset_request_execution_mode(token)
    return {
        "issues": issues,
        "capabilities": len(drafts),
        "snapshot_version": snap.version if snap else None,
        "routable": len(snap.capabilities) if snap else 0,
    }


@router.post("/admin/registry/preview-route")
async def post_preview_route(
    body: RegistryPreviewRouteRequest, _: AuthAdmin = Depends(require_admin),
) -> dict:
    """§8.5 full-chain query dry-run against the ACTIVE (Registry, Index)
    pair: Matcher → Recall → ToolIntentModel → Binder → Final Route.
    Side-effect-free by construction (the funnel never touches run_tool, 8.8)
    and writes nothing; every embedding/LLM call it makes is billed under
    ``execution_mode=preview`` (8.14). Read-only like GET /preview, so no
    audit row — the routing event itself lands with mode=preview (8.12)."""
    import types as _types

    from api.deps import llm
    from core.application.chat.intent_funnel import funnel

    deps = _types.SimpleNamespace(
        session_factory=SessionLocal, embedder=_embedder, llm=llm,
    )
    return await funnel.preview(body.query.strip(), deps=deps)


@router.post("/admin/registry/publish")
async def post_publish(
    body: RegistryPublishRequest, admin: AuthAdmin = Depends(require_admin)
) -> dict:
    try:
        view = await publish_draft(
            _embedder(), actor_username=admin.username, note=body.note,
            session_factory=SessionLocal,
        )
    except PublishRejectedError as exc:
        # the rejection itself is the audit-worthy event (no version row exists)
        await audit("publish_rejected", actor_username=admin.username, ok=False,
                    detail={"issues": exc.issues}, session_factory=SessionLocal)
        raise HTTPException(status_code=422, detail={"issues": exc.issues}) from exc
    except (RegistryConflictError, RegistryStateError) as exc:
        await audit("publish", actor_username=admin.username, ok=False,
                    detail={"error": str(exc)}, session_factory=SessionLocal)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await audit("publish", actor_username=admin.username, target=f"v{view.version}",
                detail={"fingerprint": view.fingerprint, "capabilities": len(view.entries),
                        "note": body.note}, session_factory=SessionLocal)
    return {"version": view.version, "state": view.state, "fingerprint": view.fingerprint}


@router.get("/admin/registry/versions")
async def get_versions(_: AuthAdmin = Depends(require_admin)) -> dict:
    versions = await list_versions(session_factory=SessionLocal)
    return {
        "versions": [
            {
                "version": v.version, "state": v.state, "fingerprint": v.fingerprint,
                "source_version": v.source_version, "actor_username": v.actor_username,
                "note": v.note, "error": v.error,
                "capabilities": len(v.entries),
                "routable": len(v.capabilities),
            }
            for v in versions
        ]
    }


@router.get("/admin/registry/versions/{version}")
async def get_one_version(version: int, _: AuthAdmin = Depends(require_admin)) -> dict:
    try:
        v = await get_version(version, session_factory=SessionLocal)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "version": v.version, "state": v.state, "fingerprint": v.fingerprint,
        "source_version": v.source_version, "actor_username": v.actor_username,
        "note": v.note, "error": v.error,
        "entries": [e.to_payload() for e in v.entries],
    }


@router.post("/admin/registry/versions/{version}/rollback")
async def post_rollback(
    version: int, body: RegistryRollbackRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    try:
        view = await rollback(
            version, actor_username=admin.username, session_factory=SessionLocal,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RegistryConflictError, RegistryStateError, RegistryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await audit("rollback", actor_username=admin.username, target=f"v{view.version}",
                detail={"from_version": version, "fingerprint": view.fingerprint},
                session_factory=SessionLocal)
    return {"version": view.version, "state": view.state, "source_version": version}


# ── audit read (admin visibility of the trail itself) ─────────────────────────────

@router.get("/admin/registry/audit")
async def get_audit(
    limit: int = 100, _: AuthAdmin = Depends(require_admin),
) -> dict:
    return {"entries": await list_audit(limit=min(limit, 500), session_factory=SessionLocal)}
