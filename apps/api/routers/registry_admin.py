"""Intent Registry console (admin) — the LIVE-table plane (migration 0014).

No Draft, no Publish, no Projection: an admin write goes straight into
``capabilities`` / ``capability_standard_queries`` / ``capability_similar_queries``
/ ``capability_negatives`` and is live on the next turn (invalidate_cache +
content marker make a stale corpus impossible). What the old lifecycle
guaranteed is re-established here as write discipline:

* every mutation is gated by ``validate_entries`` against the LIVE
  ``ToolRuntime.schemas()`` roster projection (intent is never authorization;
  the roster is the ONE tool existence/schema truth since the 2026-09-26
  ruling — the legacy L0 ``DIRECT_TOOLS`` table is not consulted here);
* every accepted mutation first appends the pre-change content to
  ``registry_versions`` (history only; rollback = restore a snapshot +
  re-embed) and is audit-recorded;
* query writes are embed-then-write (text + vector are one unit).

Permission posture (ratified, unchanged): no new RBAC — every route rides the
console admin gate (``require_admin``) and the admin username is the audit
actor on every mutation.
"""
from __future__ import annotations

from dataclasses import replace

from api.auth import AuthAdmin, require_admin
from api.deps import _embedder
from api.schemas import (
    RegistryCapabilityCreateRequest,
    RegistryCapabilityUpdateRequest,
    RegistryNegativeQueryRequest,
    RegistryPreviewRouteRequest,
    RegistryQueryEnabledRequest,
    RegistryQueryTextRequest,
    RegistryRollbackRequest,
    RegistrySimilarQueryRequest,
    RegistryStandardQueryRequest,
)
from core.application.chat.intent_funnel.registry import (
    CapabilityEntry,
    RegistryConflictError,
    RegistryError,
    RegistryLiveView,
    RegistryNotFoundError,
    active_view,
    audit,
    create_capability,
    derive_language,
    embedding_status,
    get_capability,
    get_version,
    list_audit,
    list_capabilities,
    list_versions,
    load_live_view,
    queries,
    snapshot_history,
    update_capability,
    validate_entries,
    version_entries,
)
from core.application.chat.intent_funnel.registry.catalog import get_catalog, list_catalog
from core.application.chat.intent_funnel.registry.entry import QueryRecord
from core.application.chat.intent_funnel.registry.store import CAPABILITY_PATCH_FIELDS
from core.infrastructure.db import SessionLocal
from fastapi import APIRouter, Depends, HTTPException, Query

router = APIRouter(tags=["registry-admin"])


def _entry_json(e: CapabilityEntry) -> dict:
    return {**e.to_payload(), "row_version": e.row_version}


def _roster() -> dict[str, dict[str, int]]:
    """The validation source of truth (final ruling): the LIVE tool roster
    projected from ``ToolRuntime.schemas()`` — tool name -> {slot: max_len}
    (0 = the runtime schema states no length bound; the gate then compares
    slot names only)."""
    from api.deps import get_agent_kernel

    out: dict[str, dict[str, int]] = {}
    for s in get_agent_kernel().runtime.schemas():
        props = (s.get("parameters") or {}).get("properties") or {}
        out[str(s["name"])] = {
            str(k): int((v or {}).get("maxLength") or 0)
            for k, v in props.items()
        }
    return out


async def _gated(entries: list[CapabilityEntry]) -> None:
    """The validation gate in front of every write: issues == [] or 422."""
    issues = validate_entries(entries, tool_schemas=_roster())
    if issues:
        raise HTTPException(status_code=422, detail={"issues": issues})


def _override(view: RegistryLiveView, entry: CapabilityEntry) -> list[CapabilityEntry]:
    """The live entry set with one entry projected to its post-write form."""
    hit = False
    out = []
    for e in view.entries:
        if e.capability_id == entry.capability_id:
            out.append(entry)
            hit = True
        else:
            out.append(e)
    if not hit:
        out.append(entry)
    return out


# ── capabilities (intent information; live) ──────────────────────────────────────

@router.get("/admin/registry/capabilities")
async def get_capabilities(_: AuthAdmin = Depends(require_admin)) -> dict:
    caps = await list_capabilities(session_factory=SessionLocal)
    return {"capabilities": [_entry_json(e) for e in caps]}


@router.get("/admin/registry/capabilities/{capability_id}")
async def get_one_capability(
    capability_id: str, _: AuthAdmin = Depends(require_admin)
) -> dict:
    entry = await get_capability(capability_id, session_factory=SessionLocal)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no capability {capability_id!r}")
    return {"capability": _entry_json(entry)}


@router.post("/admin/registry/capabilities")
async def post_capability(
    body: RegistryCapabilityCreateRequest, admin: AuthAdmin = Depends(require_admin)
) -> dict:
    entry = CapabilityEntry(
        capability_id=body.capability_id.strip(),
        tool_binding=body.tool_binding.strip(),
        description=body.description,
        patterns=tuple(body.patterns), aliases=tuple(body.aliases),
        request_query_examples=tuple(body.request_query_examples),
        parameters=dict(body.parameters), arg_slots=dict(body.arg_slots),
        permissions=body.permissions, execution_policy=body.execution_policy,
        intent_kind=body.intent_kind, enabled=body.enabled, status=body.status,
        replacement_capability_id=body.replacement_capability_id,
    )
    view = await load_live_view(session_factory=SessionLocal)
    await _gated(_override(view, entry))
    await snapshot_history(view.entries, actor_username=admin.username,
                           note=f"before create {entry.capability_id}",
                           session_factory=SessionLocal)
    try:
        created = await create_capability(entry, session_factory=SessionLocal)
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await audit("capability_create", actor_username=admin.username,
                target=entry.capability_id, session_factory=SessionLocal)
    return {"capability": _entry_json(created)}


@router.patch("/admin/registry/capabilities/{capability_id}")
async def patch_capability(
    capability_id: str, body: RegistryCapabilityUpdateRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    patch = dict(body.patch)
    unknown = set(patch) - set(CAPABILITY_PATCH_FIELDS)
    if unknown:
        raise HTTPException(status_code=422,
                            detail=f"non-capability fields in patch: {sorted(unknown)}")
    current = await get_capability(capability_id, session_factory=SessionLocal)
    if current is None:
        raise HTTPException(status_code=404, detail=f"no capability {capability_id!r}")
    projected = replace(
        current,
        **{k: (tuple(v) if k in ("patterns", "aliases", "request_query_examples")
               else v) for k, v in patch.items()},
    )
    view = await load_live_view(session_factory=SessionLocal)
    await _gated(_override(view, projected))
    await snapshot_history(view.entries, actor_username=admin.username,
                           note=f"before update {capability_id}",
                           session_factory=SessionLocal)
    try:
        updated = await update_capability(
            capability_id, patch, body.expected_row_version,
            session_factory=SessionLocal,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit("capability_update", actor_username=admin.username,
                target=capability_id,
                detail={"patch": patch, "row_version": updated.row_version},
                session_factory=SessionLocal)
    return {"capability": _entry_json(updated)}


# ── Tool Schema (runtime truth, read-only) ────────────────────────────────────────
# The Advanced tab renders PARAMETERS from the SAME schema the Agent loop hands the
# model (``ToolRuntime.schemas()`` -> ``ToolDefinition.parameters`` JSON Schema).
# This endpoint is a projection, not a second parameter model: it writes nothing,
# and it is also the roster the validation gate above cross-checks against.

@router.get("/admin/registry/tool-schemas")
async def get_tool_schemas(_: AuthAdmin = Depends(require_admin)) -> dict:
    from api.deps import get_agent_kernel

    schemas = get_agent_kernel().runtime.schemas()
    return {"tools": [
        {
            "name": s["name"],
            "description": s.get("description", ""),
            "parameters": s.get("parameters", {}),
        }
        for s in sorted(schemas, key=lambda x: x["name"])
    ]}


# ── Action Catalog (inventory only, migration 0011) ──────────────────────────────
# The Catalog is the ACTION UNIVERSE: what the system can do. It never feeds
# routing — join to the Registry is a read-time tool_binding join, and a
# not-yet-registered action becomes routable only after a capability row is
# created (from the Catalog, below) AND its corpus is curated AND it is
# enabled: 入表≠开闸, three independent switches.

async def _catalog_view() -> dict:
    rows = await list_catalog(session_factory=SessionLocal)
    caps = await list_capabilities(session_factory=SessionLocal)
    by_tool = {c.tool_binding: c for c in caps}
    actions = []
    for r in rows:
        c = by_tool.get(r["tool_binding"])
        if c is None:
            funnel, current_route = "not registered", "agent"
        elif c.enabled and c.status == "active":
            funnel = "enabled"
            current_route = "funnel" if c.intent_kind == "action" else f"funnel ({c.intent_kind})"
        else:
            funnel = c.status
            current_route = "agent"
        actions.append({
            **r,
            "bindable": r["tool_binding"] in _roster(),
            "registered": c is not None,
            "capability_id": c.capability_id if c else None,
            "registry_status": c.status if c else None,
            "registry_enabled": c.enabled if c else None,
            "intent_kind": c.intent_kind if c else None,
            "funnel": funnel,
            "current_route": current_route,
            "registry_corpus": ({
                "standard": len(c.standard_queries),
                "similar": len(c.similar_queries),
                "negatives": len(c.negatives),
            } if c is not None else None),
        })
    catalog_tools = {r["tool_binding"] for r in rows}
    for c in caps:  # capabilities without a catalog row must still show up
        if c.tool_binding in catalog_tools:
            continue
        actions.append({
            "action_key": c.capability_id, "display_name": c.capability_id,
            "description": c.description, "tool_binding": c.tool_binding,
            "route": "agent", "implementation_ref": "(no catalog row)",
            "status": "user_facing",
            "bindable": c.tool_binding in _roster(),
            "registered": True, "capability_id": c.capability_id,
            "registry_status": c.status, "registry_enabled": c.enabled,
            "intent_kind": c.intent_kind,
            "funnel": "enabled" if (c.enabled and c.status == "active") else c.status,
            "current_route": "funnel" if (c.enabled and c.status == "active") else "agent",
            "registry_corpus": {
                "standard": len(c.standard_queries),
                "similar": len(c.similar_queries),
                "negatives": len(c.negatives),
            },
        })
    return {"actions": actions}


@router.get("/admin/registry/catalog")
async def get_catalog_view(_: AuthAdmin = Depends(require_admin)) -> dict:
    return await _catalog_view()


@router.post("/admin/registry/catalog/{action_key}/create")
async def post_catalog_create(
    action_key: str, admin: AuthAdmin = Depends(require_admin)
) -> dict:
    """Create the capability row from a Catalog entry — the admission step.
    The row is born DISABLED with an empty corpus: it becomes routable only
    after its Standard queries (zh + en) are curated and it is enabled.
    Parameters default to the mechanical projection of the LIVE runtime tool
    schema (``ToolRuntime.schemas()`` — the one existence/schema truth since
    the 2026-09-26 ruling; a tool absent from the roster is not executable)."""
    me = await get_catalog(action_key, session_factory=SessionLocal)
    if me is None:
        raise HTTPException(status_code=404, detail=f"unknown action {action_key!r}")
    slots = _roster().get(me["tool_binding"])
    if slots is None:
        raise HTTPException(
            status_code=409,
            detail=f"tool {me['tool_binding']!r} is not a registered ToolRuntime "
                   "executable — Registry cannot invent executables. Runtime "
                   "admission of this tool is a separate task.",
        )
    if await get_capability(f"cap-{action_key}", session_factory=SessionLocal) is not None:
        raise HTTPException(status_code=409, detail=f"{action_key} is already registered")
    parameters = {
        slot: {
            "type": "string",
            "description": f"{slot} argument of {me['tool_binding']}",
            "required": True,
            **({"max_len": bound} if bound > 0 else {}),
        }
        for slot, bound in slots.items()
    }
    entry = CapabilityEntry(
        capability_id=f"cap-{action_key}",
        tool_binding=me["tool_binding"],
        description=me["description"] or me["display_name"],
        parameters=parameters,
        intent_kind="action",
        enabled=False, status="disabled",
    )
    view = await load_live_view(session_factory=SessionLocal)
    await _gated(_override(view, entry))
    await snapshot_history(view.entries, actor_username=admin.username,
                           note=f"before catalog-create {entry.capability_id}",
                           session_factory=SessionLocal)
    created = await create_capability(entry, session_factory=SessionLocal)
    await audit("catalog_create", actor_username=admin.username,
                target=created.capability_id,
                detail={"action_key": action_key, "from": "action_catalog"},
                session_factory=SessionLocal)
    return {"capability": _entry_json(created)}


# ── query corpus plane (embed-then-write, atomic) ────────────────────────────────

@router.get("/admin/registry/capabilities/{capability_id}/queries")
async def get_queries(capability_id: str,
                      _: AuthAdmin = Depends(require_admin)) -> dict:
    try:
        return await queries.list_queries(capability_id, session_factory=SessionLocal)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _entry_of(capability_id: str) -> CapabilityEntry:
    entry = await get_capability(capability_id, session_factory=SessionLocal)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no capability {capability_id!r}")
    return entry


@router.post("/admin/registry/capabilities/{capability_id}/queries/standard")
async def post_standard_query(
    capability_id: str, body: RegistryStandardQueryRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    entry = await _entry_of(capability_id)
    text = body.query.strip()
    provisional = replace(
        entry,
        standard_queries=entry.standard_queries + (QueryRecord(
            id="pending", query=text, language=derive_language(text),
            position=body.position),),
    )
    view = await load_live_view(session_factory=SessionLocal)
    await _gated(_override(view, provisional))
    await snapshot_history(view.entries, actor_username=admin.username,
                           note=f"before add standard {capability_id}",
                           session_factory=SessionLocal)
    try:
        saved = await queries.add_standard_query(
            capability_id, text, embedder=_embedder(),
            position=body.position, session_factory=SessionLocal)
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RegistryError as exc:  # embedder failure: NOTHING was written
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit("query_add", actor_username=admin.username, target=capability_id,
                detail={"table": "standard", "id": saved["id"], "language": saved["language"]},
                session_factory=SessionLocal)
    return {"query": saved}


@router.post("/admin/registry/capabilities/{capability_id}/queries/similar")
async def post_similar_query(
    capability_id: str, body: RegistrySimilarQueryRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    entry = await _entry_of(capability_id)
    text = body.query.strip()
    provisional = replace(
        entry,
        similar_queries=entry.similar_queries + (QueryRecord(
            id="pending", query=text, language=derive_language(text),
            position=body.position,
            standard_query_id=body.standard_query_id),),
    )
    view = await load_live_view(session_factory=SessionLocal)
    await _gated(_override(view, provisional))
    await snapshot_history(view.entries, actor_username=admin.username,
                           note=f"before add similar {capability_id}",
                           session_factory=SessionLocal)
    try:
        saved = await queries.add_similar_query(
            body.standard_query_id, text, embedder=_embedder(),
            position=body.position, session_factory=SessionLocal)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RegistryError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit("query_add", actor_username=admin.username, target=capability_id,
                detail={"table": "similar", "id": saved["id"], "language": saved["language"]},
                session_factory=SessionLocal)
    return {"query": saved}


@router.post("/admin/registry/capabilities/{capability_id}/queries/negative")
async def post_negative_query(
    capability_id: str, body: RegistryNegativeQueryRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    await _entry_of(capability_id)  # existence check
    view = await load_live_view(session_factory=SessionLocal)
    await snapshot_history(view.entries, actor_username=admin.username,
                           note=f"before add negative {capability_id}",
                           session_factory=SessionLocal)
    try:
        saved = await queries.add_negative_query(
            capability_id, body.query, position=body.position,
            session_factory=SessionLocal)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit("query_add", actor_username=admin.username, target=capability_id,
                detail={"table": "negative", "id": saved["id"]},
                session_factory=SessionLocal)
    return {"query": saved}


async def _owner_of(table: str, query_id: str) -> CapabilityEntry:
    """The capability owning a query row (for the gate projection). Negatives
    are not match data and need no projection — they resolve by capability."""
    if table not in ("standard", "similar"):
        raise HTTPException(status_code=422,
                            detail="text edits are for standard/similar rows")
    caps = await list_capabilities(session_factory=SessionLocal)
    for c in caps:
        pool = c.standard_queries if table == "standard" else c.similar_queries
        if any(r.id == query_id for r in pool):
            return c
    raise HTTPException(status_code=404, detail=f"no {table} query {query_id!r}")


@router.patch("/admin/registry/queries/{table}/{query_id}")
async def patch_query_text(
    table: str, query_id: str, body: RegistryQueryTextRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    entry = await _owner_of(table, query_id)
    text = body.query.strip()
    language = derive_language(text)
    field = "standard_queries" if table == "standard" else "similar_queries"
    rows = tuple(
        replace(r, query=text, language=language) if r.id == query_id else r
        for r in getattr(entry, field)
    )
    view = await load_live_view(session_factory=SessionLocal)
    await _gated(_override(view, replace(entry, **{field: rows})))
    await snapshot_history(view.entries, actor_username=admin.username,
                           note=f"before text edit {table}/{query_id}",
                           session_factory=SessionLocal)
    try:
        saved = await queries.update_query_text(
            table, query_id, text, embedder=_embedder(),
            session_factory=SessionLocal)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RegistryError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit("query_update", actor_username=admin.username,
                target=f"{table}/{query_id}",
                detail={"language": saved["language"]}, session_factory=SessionLocal)
    return {"query": saved}


@router.post("/admin/registry/queries/{table}/{query_id}/enabled")
async def patch_query_enabled(
    table: str, query_id: str, body: RegistryQueryEnabledRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    entry: CapabilityEntry | None = None
    if table in ("standard", "similar"):
        entry = await _owner_of(table, query_id)
        field = "standard_queries" if table == "standard" else "similar_queries"
        rows = tuple(
            replace(r, enabled=body.enabled) if r.id == query_id else r
            for r in getattr(entry, field)
        )
        view = await load_live_view(session_factory=SessionLocal)
        await _gated(_override(view, replace(entry, **{field: rows})))
        await snapshot_history(view.entries, actor_username=admin.username,
                               note=f"before enable {table}/{query_id}",
                               session_factory=SessionLocal)
    else:
        view = await load_live_view(session_factory=SessionLocal)
        await snapshot_history(view.entries, actor_username=admin.username,
                               note=f"before enable {table}/{query_id}",
                               session_factory=SessionLocal)
    try:
        saved = await queries.set_query_enabled(
            table, query_id, body.enabled, session_factory=SessionLocal)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit("query_toggle", actor_username=admin.username,
                target=f"{table}/{query_id}",
                detail={"enabled": body.enabled}, session_factory=SessionLocal)
    return {"query": saved}


@router.delete("/admin/registry/queries/{table}/{query_id}")
async def delete_query_row(
    table: str, query_id: str,
    cascade_similar: bool = Query(default=False),
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    if table in ("standard", "similar"):
        entry = await _owner_of(table, query_id)
        if table == "standard":
            projected = replace(
                entry,
                standard_queries=tuple(r for r in entry.standard_queries
                                       if r.id != query_id),
                similar_queries=tuple(
                    s for s in entry.similar_queries
                    if s.standard_query_id != query_id),
            )
        else:
            projected = replace(
                entry,
                similar_queries=tuple(r for r in entry.similar_queries
                                      if r.id != query_id),
            )
        view = await load_live_view(session_factory=SessionLocal)
        await _gated(_override(view, projected))
        await snapshot_history(view.entries, actor_username=admin.username,
                               note=f"before delete {table}/{query_id}",
                               session_factory=SessionLocal)
    else:
        view = await load_live_view(session_factory=SessionLocal)
        await snapshot_history(view.entries, actor_username=admin.username,
                               note=f"before delete {table}/{query_id}",
                               session_factory=SessionLocal)
    try:
        saved = await queries.delete_query(
            table, query_id, cascade_similar=cascade_similar,
            session_factory=SessionLocal)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit("query_delete", actor_username=admin.username,
                target=f"{table}/{query_id}", session_factory=SessionLocal)
    return {"deleted": saved}


# ── corpus / embedding state (honest reporting) ──────────────────────────────────

@router.get("/admin/registry/embedding-status")
async def get_embedding_status(_: AuthAdmin = Depends(require_admin)) -> dict:
    status = await embedding_status(session_factory=SessionLocal)
    status["pending"] = len(await queries.pending_embeddings(
        session_factory=SessionLocal))
    return status


@router.get("/admin/registry/validate")
async def get_validate(_: AuthAdmin = Depends(require_admin)) -> dict:
    """The check button: validate the LIVE set against the roster, zero writes."""
    caps = await list_capabilities(session_factory=SessionLocal)
    return {"capabilities": len(caps),
            "issues": validate_entries(caps, tool_schemas=_roster())}


@router.get("/admin/registry/live-view")
async def get_live_view(_: AuthAdmin = Depends(require_admin)) -> dict:
    """What the RUNTIME sees right now: the active live view's fingerprint and
    per-capability routability (enabled+active) — plus the Recall index state."""
    view = await active_view(session_factory=SessionLocal)
    return {
        "fingerprint": view.fingerprint if view else None,
        "capabilities": [
            {"capability_id": e.capability_id, "routable": bool(e.enabled and e.status == "active"),
             "corpus": len(e.intent_corpus)}
            for e in (view.entries if view else ())
        ],
    }


# ── preview (§8.5 full-chain dry-run) ────────────────────────────────────────────

@router.post("/admin/registry/preview-route")
async def post_preview_route(
    body: RegistryPreviewRouteRequest, _: AuthAdmin = Depends(require_admin),
) -> dict:
    """Matcher -> Recall -> ToolIntentModel -> Binder against the LIVE tables.
    Side-effect-free by construction (the funnel never touches run_tool, 8.8)
    and writes nothing; every embedding/LLM call it makes is billed under
    ``execution_mode=preview`` (8.14)."""
    import types as _types

    from api.deps import llm
    from core.application.chat.intent_funnel import funnel

    deps = _types.SimpleNamespace(
        session_factory=SessionLocal, embedder=_embedder, llm=llm,
    )
    return await funnel.preview(body.query.strip(), deps=deps)


# ── registry_versions: HISTORY only (rollback = restore + re-embed) ─────────────

@router.get("/admin/registry/versions")
async def get_versions(_: AuthAdmin = Depends(require_admin)) -> dict:
    return {"versions": await list_versions(session_factory=SessionLocal)}


@router.get("/admin/registry/versions/{version}")
async def get_one_version(version: int, _: AuthAdmin = Depends(require_admin)) -> dict:
    try:
        return await get_version(version, session_factory=SessionLocal)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _restore_corpus(cap_id: str, target: CapabilityEntry, embedder) -> None:
    """Corpus reconciliation of one capability against a history snapshot:
    every sentence write goes through the embed-then-write query plane."""
    cur = await queries.list_queries(cap_id, session_factory=SessionLocal)
    for r in cur["similar"]:  # similars first: they ride on their Standard
        await queries.delete_query("similar", r["id"], session_factory=SessionLocal)
    cur_std = {r["language"]: r for r in cur["standard"]}
    want_std = {r.language: r for r in target.standard_queries}
    std_id_by_lang: dict[str, str] = {}
    for lang, w in want_std.items():
        if lang in cur_std:
            row = cur_std[lang]
            std_id_by_lang[lang] = row["id"]
            if row["query"] != w.query:
                await queries.update_query_text("standard", row["id"], w.query,
                                                embedder=embedder,
                                                session_factory=SessionLocal)
            if row["enabled"] != w.enabled:
                await queries.set_query_enabled("standard", row["id"], w.enabled,
                                                session_factory=SessionLocal)
        else:
            added = await queries.add_standard_query(cap_id, w.query,
                                                     embedder=embedder,
                                                     position=w.position,
                                                     session_factory=SessionLocal)
            std_id_by_lang[lang] = added["id"]
    for lang, row in cur_std.items():
        if lang not in want_std:
            await queries.delete_query("standard", row["id"],
                                       session_factory=SessionLocal)
    for s in target.similar_queries:
        std_id = std_id_by_lang.get(s.language)
        if std_id is None:  # snapshot-internal inconsistency: orphan similar
            continue
        added = await queries.add_similar_query(std_id, s.query, embedder=embedder,
                                                position=s.position,
                                                session_factory=SessionLocal)
        if not s.enabled:
            await queries.set_query_enabled("similar", added["id"], False,
                                            session_factory=SessionLocal)
    # negatives: no embeddings, plain rows — rebuild to the snapshot's set
    neg_cur = await queries.list_queries(cap_id, session_factory=SessionLocal)
    for r in neg_cur["negative"]:
        await queries.delete_query("negative", r["id"], session_factory=SessionLocal)
    for pos, text in enumerate(target.negatives):
        await queries.add_negative_query(cap_id, text, position=pos,
                                         session_factory=SessionLocal)


def _intent_patch(current: CapabilityEntry, target: CapabilityEntry) -> dict:
    patch: dict = {}
    for f in CAPABILITY_PATCH_FIELDS:
        cv, tv = getattr(current, f), getattr(target, f)
        if f in ("patterns", "aliases", "request_query_examples"):
            cv, tv = list(cv or []), list(tv or [])
        elif f in ("parameters", "arg_slots"):
            cv, tv = dict(cv or {}), dict(tv or {})
        if cv != tv:
            patch[f] = tv
    return patch


@router.post("/admin/registry/versions/{version}/rollback")
async def post_rollback(
    version: int, body: RegistryRollbackRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    """Restore = replay a history snapshot into the live tables + re-embed.
    Capabilities absent from the snapshot are SOFT-DISABLED (no hard delete).
    Embedder failure mid-way leaves a partial restore and is reported — an
    honest 503, never a fake success."""
    try:
        targets = await version_entries(version, session_factory=SessionLocal)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    embedder = _embedder()
    view = await load_live_view(session_factory=SessionLocal)
    await snapshot_history(view.entries, actor_username=admin.username,
                           note=f"before rollback to v{version}",
                           session_factory=SessionLocal)
    target_ids = {t.capability_id for t in targets}
    restored = 0
    try:
        for t in targets:
            current = await get_capability(t.capability_id,
                                           session_factory=SessionLocal)
            if current is None:
                await create_capability(replace(t, enabled=False, status="disabled"),
                                        session_factory=SessionLocal)
                current = await get_capability(t.capability_id,
                                               session_factory=SessionLocal)
            await _restore_corpus(t.capability_id, t, embedder)
            current = await get_capability(t.capability_id,
                                           session_factory=SessionLocal)
            patch = _intent_patch(current, t)  # enabled/status land last
            if patch:
                await update_capability(t.capability_id, patch,
                                        current.row_version,
                                        session_factory=SessionLocal)
            restored += 1
        for e in view.entries:  # live rows outside the snapshot: soft-disable
            if e.capability_id in target_ids:
                continue
            if e.enabled or e.status != "disabled":
                fresh = await get_capability(e.capability_id,
                                             session_factory=SessionLocal)
                await update_capability(e.capability_id,
                                        {"enabled": False, "status": "disabled"},
                                        fresh.row_version,
                                        session_factory=SessionLocal)
    except (RegistryError, RegistryConflictError, RegistryNotFoundError) as exc:
        await audit("rollback", actor_username=admin.username, ok=False,
                    target=f"v{version}", detail={"error": str(exc)},
                    session_factory=SessionLocal)
        raise HTTPException(status_code=503,
                            detail=f"partial restore at v{version}: {exc}") from exc
    final = await list_capabilities(session_factory=SessionLocal)
    issues = validate_entries(final, tool_schemas=_roster())
    await audit("rollback", actor_username=admin.username, target=f"v{version}",
                detail={"restored": restored, "issues": issues},
                session_factory=SessionLocal)
    return {"restored_version": version, "capabilities": restored, "issues": issues}


# ── audit read (admin visibility of the trail itself) ─────────────────────────────

@router.get("/admin/registry/audit")
async def get_audit(
    limit: int = 100, _: AuthAdmin = Depends(require_admin),
) -> dict:
    return {"entries": await list_audit(limit=min(limit, 500), session_factory=SessionLocal)}
