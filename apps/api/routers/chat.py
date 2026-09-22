"""Chat routes: a thin HTTP/SSE adapter over the chat control plane.

Business orchestration lives in ``core.application.chat`` (TurnOrchestrator +
executors); this module only resolves the HTTP identity/channel (transport
concern), wires :class:`ChatDeps` from the module globals (preserving the existing
test seams), and translates orchestrator events into SSE frames. The
query-repository import endpoints are unchanged.
"""
from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from uuid import UUID

from agent.security.approvals import get_approval_bridge
from api.auth import AuthUser, require_user, require_user_optional
from api.deps import (
    _batch_embedder,
    _embedder,
    get_agent,
    get_drive_service,
    get_retriever,
    get_task_queue,
    llm,
)
from api.routers._shared import (
    _guest_quota,
    _log_usage,
    _resolve_chat_route,
    resolve_guest_identity,
)
from api.schemas import ChatImportRequest, ChatRequest, ChatSessionImportRequest
from api.viewer_context import (
    build_viewer_blocks,
    render_viewer_reference,
    validate_viewer_citations,
    viewer_citation_map,
    viewer_snapshot,
)
from core.application.chat.context import (
    ResearchConflict,
    assemble_turn_history,
    build_turn_context,
)

# Re-exports under the historic private names: tests (and sibling consumers) import
# these off ``api.routers.chat``; F401 cannot see the string-level import sites.
from core.application.chat.context import (
    asset_readable as _asset_readable,  # noqa: F401
)
from core.application.chat.context import (
    build_viewer_assembly as _build_viewer_assembly_core,
)
from core.application.chat.context import (
    resolve_research_context as _resolve_research_context,
)
from core.application.chat.context import (
    viewer_abort as _viewer_abort,  # noqa: F401
)
from core.application.chat.executors.base import ChatDeps, ViewerDeps
from core.application.chat.lifecycle import (
    extract_retrieval as _extract_retrieval,  # noqa: F401
)
from core.application.chat.lifecycle import (
    last_written_id as _last_written_id,  # noqa: F401
)
from core.application.chat.lifecycle import (
    maybe_continue_research as _maybe_continue_research,  # noqa: F401
)
from core.application.chat.lifecycle import persist_turn_meta as _persist_meta_impl
from core.application.chat.lifecycle import viewer_post_turn as _viewer_post_turn_core
from core.application.chat.turn_orchestrator import TurnOrchestrator
from core.application.drive_service import DriveError, DriveService
from core.config import settings
from core.infrastructure.db import (
    ChunkModel,
    LoginTokenModel,
    MessageModel,
    SessionLocal,
    SessionModel,
)
from core.infrastructure.drive_repositories import SqlChunkRepository
from core.infrastructure.ingest import build_chunks, write_query_repo_chunks
from core.infrastructure.jobs import CHAT_SESSION_IMPORT, TaskQueue
from core.infrastructure.memory import SessionMemoryStore
from core.infrastructure.request_context import (
    set_request_llm_channel,
    set_request_user,
)
from core.infrastructure.security import authorize_usage, get_role
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

router = APIRouter(tags=["chat"])

logger = logging.getLogger(__name__)


# ── Test-seam wrappers: the moved implementations take their session factory / llm
# as explicit args; these module-level names stay the patchable surface AND back
# the per-request ChatDeps wiring below.

_VIEWER_DEPS = ViewerDeps(
    build_blocks=build_viewer_blocks,
    validate_citations=validate_viewer_citations,
    citation_map=viewer_citation_map,
    snapshot=viewer_snapshot,
    render_reference=render_viewer_reference,
)


async def _persist_turn_meta(message_id: str | None, key: str, value) -> None:
    """SessionLocal at CALL time — so monkeypatched seams keep working (see lifecycle)."""
    await _persist_meta_impl(SessionLocal, message_id, key, value)


async def _build_viewer_assembly(body: ChatRequest, drive: DriveService, user_id) -> dict | None:
    """Compat wrapper (legacy 3-arg signature) over the core viewer assembler."""
    return await _build_viewer_assembly_core(body, drive, user_id, _VIEWER_DEPS)


async def _viewer_post_turn(
    assembly: dict | None, body: ChatRequest, answer: str,
    messages: list[dict] | None,
    user_message_id: str | None, assistant_message_id: str | None,
) -> dict | None:
    """Compat wrapper (legacy 6-arg signature); persistence dispatches through the
    patchable module-global ``_persist_turn_meta`` at call time."""
    deps = SimpleNamespace(persist_turn_meta=_persist_turn_meta, viewer=_VIEWER_DEPS)
    return await _viewer_post_turn_core(
        deps, assembly, body, answer, messages,
        user_message_id, assistant_message_id,
    )


async def _assemble_turn_history(
    body: ChatRequest,
    session_memory: SessionMemoryStore,
    session_id,
    user_text: str,
    *,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
) -> tuple[list[dict], dict | None, str | None]:
    """Compat wrapper delegating to the core history assembler (single implementation).

    Signature and module-global lookups (``SessionLocal`` / ``llm``) preserved for the
    session-memory v2 router-assembly tests — the seam reads the globals at call time.
    """
    deps = SimpleNamespace(session_factory=SessionLocal, llm=llm)
    return await assemble_turn_history(
        body, session_memory, session_id, user_text,
        deps=deps, model=model, base_url=base_url, api_key=api_key,
    )


# ── Transport: identity + quota + LLM channel (previously duplicated per route) ──


async def _resolve_identity(request: Request, body: ChatRequest, user: AuthUser | None) -> dict:
    """Resolve the caller (or mint a guest), the daily quota and the pinned LLM channel.

    Raises HTTPException (429 / 503) exactly like the legacy handlers. A logged-in user
    whose every LLM key is disabled on the Tokens page has *no* usable channel — they
    still log in fine, but degrade to the anonymous tier for this request: guest daily
    quota + anonymous routing (the "equivalent to an anonymous user" behavior; full
    access returns when the admin re-enables a key). The anonymous tier with no channel
    either must NOT fall back to the legacy global connection — the admin must bind a
    channel to the role.
    """
    # Scope RAG / memory recall to this request's user (guest → public-link assets only).
    set_request_user(user.user_id if user is not None else None)
    notice = None
    guest_token = None
    tier = "free"
    async with SessionLocal() as session:
        if user is None:
            user_id, guest_token = await resolve_guest_identity(SessionLocal, body.guest_token)
            await _guest_quota(request.app.state.redis, user_id)
            token = None
            role_id = "anonymous"
            log_user = None
        else:
            user_id = user.user_id
            token = await session.get(LoginTokenModel, user.token_id)
            role_id = user.role.role_id
            log_user = user
        base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, token, role_id)
        if user is not None and not base_url and not api_key:
            anon = await get_role(session, "anonymous")
            anon_limit = anon.daily_request_limit if anon is not None else settings.guest_daily_limit
            limit_txt = f"每天限 {anon_limit} 次" if anon_limit >= 0 else "按匿名用户限额"
            await _guest_quota(
                request.app.state.redis, user_id,
                detail="你的额度已用完,且匿名额度也已用完。请充值或升级套餐后继续使用。",
            )
            role_id = "anonymous"
            log_user = None
            notice = (
                f"你的渠道额度已用完,已按匿名用户身份继续使用({limit_txt})。"
                "如需更多额度,请充值或升级套餐。"
            )
            base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, None, role_id)
        elif user is not None:
            tier = await authorize_usage(session, user.user_id, user.role)
    if not base_url and not api_key:
        raise HTTPException(
            status_code=503,
            detail="当前没有可用的 LLM 渠道,无法使用聊天。请联系管理员配置渠道,或充值/升级套餐后重试。",
        )
    # Pin this request's LLM channel so context-free sub-calls (rag_search query rewrite /
    # CRAG judge) use the same key/model as the conversation instead of the process-global
    # default client — one turn, one channel, host and worker alike. The SSE generator runs
    # later in the request's captured context, so setting it here covers every tool call.
    set_request_llm_channel((model, base_url or None, api_key or None))
    return {
        "user_id": user_id, "guest_token": guest_token, "log_user": log_user,
        "tier": tier, "notice": notice, "model": model,
        "base_url": base_url, "api_key": api_key, "business_name": business_name,
        "credential_id": credential_id,
    }


# ── Phase 5A direct-dispatch seam (see ChatDeps.run_tool contract) ──────────────────
# The ACTION executor calls the SAME ToolRuntime.execute the Agent loop uses: the
# pre-execute approval waterfall, monotonic sandbox guards and tool bodies are all
# inherited — this seam adds NO second execution framework, it only skips the ReAct
# flow control for turns L0 certified as fully-determined single-tool requests.
#
# Failure classification (side-effect boundary isolation):
#   * body raised / returned ``"preflight: …"``, unknown tool, invalid args, or ANY
#     failure of a READ-only tool  ⇒ the side effect provably did not happen →
#     ActionPreflightFailure → the executor escalates (Agent may clarify);
#   * pre-body DENIAL (approval refused / sandbox / source-policy guard) ⇒ decided,
#     terminal, no side effect → {"ok": False, "reason"} — escalating would only make
#     the Agent re-trigger the same approval prompt;
#   * any failure AFTER a MUTATING tool body was entered ⇒ state UNKNOWN → raise so
#     the executor terminates honestly (never a blind Agent retry duplicating it).
_MUTATING_DIRECT_TOOLS = frozenset({"create_folder", "add_term"})
_PRE_BODY_DENIALS = (
    "tool use denied", "sandbox denied:", "source policy:", "sandbox guard error",
    "pre-execute failed", "approval required but no approver registered",
)


async def _run_tool(tool: str, args: dict, ctx) -> dict:
    from agent.engine.context import _TURN_CTX, AgentTurn
    from agent.engine.decisions import ToolExecution
    from core.application.chat.actions import ActionPreflightFailure

    runtime = get_agent().runtime
    # Bind a turn context so the sandbox guards see this dispatch exactly like an
    # agent turn: the turn's SOURCE POLICY fence (and any handoff context) applies
    # unchanged; approvals flow through the ApprovalStore the stream pump bound.
    token = _TURN_CTX.set(AgentTurn(user_msg=ctx.user_text, context=dict(ctx.agent_context or {})))
    try:
        exec = ToolExecution(call_id="chat-action", name=tool, arguments=dict(args))
        result = await runtime.execute(exec)
    finally:
        _TURN_CTX.reset(token)

    if not result.is_error:
        return {"ok": True, "output": str(result.value if result.value is not None else "")}

    msg = (result.error.message or "") if result.error else ""
    info = dict(getattr(result.error, "info", None) or {})
    name = info.get("name")
    if name in ("unknown_tool", "invalid_args") or msg.startswith("preflight:"):
        raise ActionPreflightFailure(msg)
    if tool not in _MUTATING_DIRECT_TOOLS:
        # Nothing this tool can do has a side effect — the Agent fallback loses nothing.
        raise ActionPreflightFailure(msg)
    if name is None and msg.startswith(_PRE_BODY_DENIALS):
        return {"ok": False, "reason": msg}
    # Mutating tool + a post-body failure class (tool_error w/o preflight prefix,
    # invalid_output, post_blocked, execute failed) → state UNKNOWN.
    raise RuntimeError(f"action post-execution failure ({tool}): {msg}")


def _build_chat_deps(queue: TaskQueue, drive: DriveService) -> ChatDeps:
    """Wire the control plane's dependency bundle from this module's globals.

    Everything is read AT REQUEST TIME, which keeps the established test seams alive
    (monkeypatching ``chat_mod.get_agent`` / ``_log_usage`` / ``SessionLocal`` / …
    before the call changes what the control plane sees).
    """
    return ChatDeps(
        session_factory=SessionLocal,
        queue=queue,
        drive=drive,
        agent=get_agent(),
        llm=llm,
        embedder=_embedder,
        viewer=_VIEWER_DEPS,
        new_approval_bridge=get_approval_bridge,
        persist_turn_meta=_persist_turn_meta,
        log_usage=_log_usage,
        resolve_research=_resolve_research_context,
        # The staged RAG branch (Phase 4) answers through the SAME cache-wrapped seam
        # the agent's rag_search tool calls — one ACL / tenant / cache surface.
        retriever=get_retriever(),
        # Phase 5A ACTION branch dispatches registered tools through the SAME runtime.
        run_tool=_run_tool,
    )


# ── Query-repository import endpoints (unchanged by the control-plane refactor) ──


@router.post("/chat/import")
async def chat_import_pair(
    body: ChatImportRequest,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    """Import one chat Q&A pair (user message + assistant reply) as a query-repo chunk.

    Stored with ``source_type='chat'`` + ``source_id=<user_message_id>`` so re-importing
    the same pair is idempotent.
    """
    async with SessionLocal() as session:
        user_msg = await session.get(MessageModel, UUID(body.user_message_id))
        asst_msg = await session.get(MessageModel, UUID(body.assistant_message_id))
        session_row = await session.get(SessionModel, UUID(body.session_id))
    if session_row is None:
        raise HTTPException(status_code=404, detail="session not found")
    # Ownership gate: a user may only import pairs from their own sessions, so a forged
    # session_id cannot pull another user's messages into the caller's query repo.
    if session_row.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="session does not belong to you")
    if user_msg is None or asst_msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    if user_msg.session_id != UUID(body.session_id) or asst_msg.session_id != user_msg.session_id:
        raise HTTPException(status_code=400, detail="messages do not belong to the given session")
    if user_msg.role != "user" or asst_msg.role != "assistant":
        raise HTTPException(status_code=400, detail="expected a user/assistant message pair")

    from rag.config_store import load_config  # lazy: rag is a sibling package

    # A chat Q&A that joins RAG keeps its screenshot in BOTH folders: the temporary chat/temp
    # copy stays (it dies with its chat when the session/message is deleted), and a stable
    # RAG/images copy is created for the corpus sharing the same object bytes — so emptying
    # chat/temp never removes an image the repo still references. The chunk meta references
    # the stable copy; best-effort, falling back to the owned chat/temp asset if copying fails.
    rag_image_id: str | None = None
    if user_msg.attach_asset_id is not None:
        try:
            rag_image_id = str(
                (await drive.copy_to_folder(user.user_id, user_msg.attach_asset_id, "RAG/images")).id
            )
        except DriveError:
            rag_image_id = str(user_msg.attach_asset_id)

    content = f"{user_msg.text}\n\n{asst_msg.text}"
    cfg = await load_config(SessionLocal)
    title = user_msg.text.strip()[:60] or "Chat Q&A"
    chunks = await build_chunks(content, cfg, doc_title=title, llm=llm)
    for c in chunks:
        c.meta = {**c.meta, "title": title, "kind": "qa", "session_id": str(user_msg.session_id)}
        if rag_image_id is not None:
            c.meta["image_ids"] = [rag_image_id]
    chunks_repo = SqlChunkRepository(SessionLocal)
    await chunks_repo.delete_by_source("chat", [str(user_msg.id)])
    res = await write_query_repo_chunks(
        SessionLocal,
        _batch_embedder(),
        chunks=chunks,
        user_id=user.user_id,
        source_type="chat",
        source_id=str(user_msg.id),
    )
    # Flip the per-message flag on both halves of the pair: the client renders "✓ Imported"
    # from these rows, and the flag is what stops a later duplicate re-import (the assistant
    # reply keeps its state even if the bound question is deleted or re-grouped).
    async with SessionLocal() as session:
        for mid in (user_msg.id, asst_msg.id):
            row = await session.get(MessageModel, mid)
            if row is not None:
                row.imported_rag = True
        await session.commit()
    return {"chunks": res["chunks"]}


@router.post("/chat/import-session")
async def chat_import_session(
    body: ChatSessionImportRequest,
    user: AuthUser = Depends(require_user),
    queue: TaskQueue = Depends(get_task_queue),
):
    """Enqueue a whole chat session: the LLM groups its Q&A turns into repo chunks."""
    async with SessionLocal() as session:
        session_row = await session.get(SessionModel, UUID(body.session_id))
    if session_row is None or session_row.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="session does not belong to you")
    job_id = await queue.enqueue(
        CHAT_SESSION_IMPORT,
        {"session_id": body.session_id, "user_id": str(user.user_id)},
        user_id=user.user_id,
    )
    return {"job_id": str(job_id)}


@router.get("/chat/imported")
async def chat_imported_status(
    session_id: UUID, user: AuthUser = Depends(require_user)
) -> dict:
    """Which Q&A pairs of a session are already in the query repo.

    Drives the persistent "✓ Imported" state on the desktop chat buttons: on session
    load the client fetches this so an already-imported pair stays disabled across
    session switches and app restarts.

    Coverage comes from the per-message ``imported_rag`` flags set on import (stable across
    message deletes / regroupings, so the state never spreads to sibling pairs). ``qa_source_ids``
    lists every flagged user message; ``session_imported`` is true when every current user
    message is flagged. ``legacy_session_imported`` marks a pre-flag whole-session import (old
    ``kind='session-qa'``) that has no per-message data — the client treats it as fully imported
    but allows a re-import to convert it to the flag model.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(MessageModel.id, MessageModel.role, MessageModel.imported_rag)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at)
            )
        ).all()
        legacy = (
            await session.execute(
                select(ChunkModel.id)
                .where(
                    ChunkModel.source_type == "chat",
                    ChunkModel.user_id == user.user_id,
                    ChunkModel.meta["session_id"].astext == str(session_id),
                    ChunkModel.meta["kind"].astext == "session-qa",
                )
                .limit(1)
            )
        ).first()
    user_msg_ids = [str(mid) for mid, role, _ in rows if role == "user"]
    flagged_ids = [str(mid) for mid, role, flag in rows if role == "user" and flag]
    legacy_session_imported = legacy is not None
    fully_covered = bool(user_msg_ids) and len(flagged_ids) == len(user_msg_ids)
    return {
        "qa_source_ids": sorted(flagged_ids),
        "session_imported": legacy_session_imported or fully_covered,
        "legacy_session_imported": legacy_session_imported,
    }


# ── Chat turn: control-plane adapters ──────────────────────────────────────────


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: AuthUser | None = Depends(require_user_optional),
    queue: TaskQueue = Depends(get_task_queue),
    drive: DriveService = Depends(get_drive_service),
):
    """One non-streaming chat turn through the control plane."""
    ident = await _resolve_identity(request, body, user)
    deps = _build_chat_deps(queue, drive)
    try:
        ctx = await build_turn_context(
            body=body, deps=deps, user=user, stream=False, log_tag=True, **ident,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ResearchConflict as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    if ctx.viewer_abort:
        return {
            "answer": None,
            "session_id": str(body.session_id) if body.session_id else None,
            "user_id": str(ctx.user_id),
            "viewer": ctx.viewer_abort,
        }
    return await TurnOrchestrator(deps).run_turn(ctx)


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    user: AuthUser | None = Depends(require_user_optional),
    queue: TaskQueue = Depends(get_task_queue),
    drive: DriveService = Depends(get_drive_service),
):
    """SSE streaming chat over the control plane (Phase 1: full-agent path, same frames).

    Emits ``{"type": "thinking"|"content"|"tool", "data": ...}`` deltas as the model reasons
    and answers, then a final ``{"type": "done", "data": {answer, session_id, user_id,
    user_message_id, assistant_message_id, notice}}`` event. A user with no usable LLM key
    degrades to the anonymous tier (guest quota), matching ``/chat``.
    """
    # Pipeline instrumentation anchors (chat-stream only): t_entry spans handler entry to
    # the SSE done frame; the pre-agent phase covers auth/quota/channel/viewer/history
    # assembly. Per-step agent latencies live in the TurnSpan audit (data/audit.jsonl).
    t_entry = time.perf_counter()
    logger.info("chat.stream-start msg_chars=%d", len(body.message or ""))
    ident = await _resolve_identity(request, body, user)
    deps = _build_chat_deps(queue, drive)
    try:
        ctx = await build_turn_context(
            body=body, deps=deps, user=user, stream=True, log_tag=False, **ident,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ResearchConflict as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    # Viewer context: assemble + permission-check pre-stream; FULL over budget / untrusted
    # full capture aborts the turn BEFORE the agent runs (no session write, no LLM call).
    if ctx.viewer_abort:
        async def abort_gen():
            yield {"data": json.dumps({"type": "viewer", "data": ctx.viewer_abort}, ensure_ascii=False)}
            yield {"data": json.dumps({"type": "done", "data": {
                "answer": None,
                "session_id": str(body.session_id) if body.session_id else None,
                "viewer": ctx.viewer_abort,
            }}, ensure_ascii=False)}
        return EventSourceResponse(abort_gen())

    logger.info("chat.stream-pre-agent duration_ms=%.0f", (time.perf_counter() - t_entry) * 1000)
    orchestrator = TurnOrchestrator(deps)

    async def gen():
        async for frame in orchestrator.stream_turn(ctx, t_entry=t_entry):
            yield frame

    return EventSourceResponse(gen())
