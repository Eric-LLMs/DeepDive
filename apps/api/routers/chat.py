"""Chat routes: the agent-driven ``/chat`` turn, SSE streaming (``/chat/stream``), and
query-repository import (single Q&A pair / whole session / imported status).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from uuid import UUID

from agent.security.approvals import (
    ApprovalStore,
    get_approval_bridge,
    set_request_approval,
)
from api.auth import AuthUser, require_user, require_user_optional
from api.deps import (
    _batch_embedder,
    _embedder,
    get_agent,
    get_drive_service,
    get_task_queue,
    llm,
)
from api.schemas import ChatImportRequest, ChatRequest, ChatSessionImportRequest
from core.application.drive_service import DriveError, DriveService
from api.routers._shared import (
    _guest_quota,
    _log_usage,
    _resolve_chat_route,
    resolve_guest_identity,
)
from api.schemas import ChatImportRequest, ChatRequest, ChatSessionImportRequest
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
from core.infrastructure.jobs import CHAT_SESSION_IMPORT, SESSION_FINALIZE, TaskQueue
from core.infrastructure.memory import (
    SessionMemoryStore,
    compact_history,
    create_session,
)
from core.infrastructure.request_context import set_request_user
from core.infrastructure.security import authorize_usage, get_role
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

router = APIRouter(tags=["chat"])


async def _attach_note(body: ChatRequest, drive: DriveService, user_id) -> str | None:
    """Build a context note for an attached cloud asset, or ``None`` when there is none.

    Attachments are read-only references: we verify the caller can read the asset, then
    prefix a ``[Attached: …]`` note to the user message so the agent knows which document
    the user is troubleshooting. The agent's existing drive tools (pdf_extract_text,
    doc_outline, …) can then fetch the bytes by ``asset_id``.
    """
    attach = body.attach or {}
    if attach.get("kind") != "asset" or user_id is None:
        return None
    asset_id = attach.get("asset_id")
    if not asset_id:
        return None
    try:
        await drive.ensure_asset_readable(user_id, UUID(asset_id))
    except DriveError as exc:
        raise HTTPException(status_code=403, detail=f"no access to the attached file: {exc}")
    name = attach.get("name") or "document"
    return f"[Attached: {name} (asset_id {asset_id})]"


@router.post("/chat/import")
async def chat_import_pair(
    body: ChatImportRequest,
    user: AuthUser = Depends(require_user),
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

    content = f"{user_msg.text}\n\n{asst_msg.text}"
    cfg = await load_config(SessionLocal)
    title = user_msg.text.strip()[:60] or "Chat Q&A"
    chunks = await build_chunks(content, cfg, doc_title=title, llm=llm)
    for c in chunks:
        c.meta = {**c.meta, "title": title, "kind": "qa", "session_id": str(user_msg.session_id)}
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


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: AuthUser | None = Depends(require_user_optional),
    queue: TaskQueue = Depends(get_task_queue),
    drive: DriveService = Depends(get_drive_service),
):
    # Scope RAG / memory recall to this request's user (guest → public-link assets only).
    set_request_user(user.user_id if user is not None else None)
    # Resolve the LLM channel for this request: a logged-in user uses the channel pinned on
    # their token at login (failing over to another active channel of the same role); a guest
    # uses the ``anonymous`` role's channels. A role with no usable channel falls back to the
    # legacy /config route (empty base_url/api_key → the configured global client).
    #
    # A logged-in user whose every LLM key is disabled on the Tokens page has *no* usable
    # channel — they still log in fine, but degrade to the anonymous tier for this request:
    # guest daily quota + anonymous routing (that's the "equivalent to an anonymous user"
    # behavior; full access returns when the admin re-enables a key).
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
        # The anonymous tier has no channel either: do NOT fall back to the legacy global
        # connection — tell the user instead (the admin must bind a channel to the role).
        if not base_url and not api_key:
            raise HTTPException(
                status_code=503,
                detail="当前没有可用的 LLM 渠道,无法使用聊天。请联系管理员配置渠道,或充值/升级套餐后重试。",
            )

    user_text = body.message
    if body.attach:
        note = await _attach_note(body, drive, user_id)
        if note:
            user_text = f"{note}\n\n{body.message}"
    session_id = body.session_id or await create_session(SessionLocal, user_id, title=body.message)
    session_memory = SessionMemoryStore(SessionLocal, _embedder(), llm, session_id, user_id)
    history = body.history or await session_memory.load_messages()
    history = await compact_history(
        history,
        session_factory=SessionLocal,
        session_id=session_id,
        session_memory=session_memory,
        llm=llm,
        model=model,
        base_url=base_url or None,
        api_key=api_key or None,
    )
    result = await get_agent().run(
        user_text,
        history,
        session_memory=session_memory,
        model=model,
        base_url=base_url or None,
        api_key=api_key or None,
    )
    # close() (inside run) already flushed events; defer the expensive embed+summary work.
    await queue.enqueue(SESSION_FINALIZE, {"session_id": str(session_id)})
    if log_user is not None:
        await _log_usage(
            log_user, business_name, "chat", result.usage,
            credential_id=credential_id, paid=(tier == "paid"),
        )
    # Resolve this turn's user-message / assistant-answer ids so the client can delete a
    # single message. Scan ascending for the last row matching each text (texts may repeat,
    # but this turn's rows are always the newest).
    user_message_id = assistant_message_id = None
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(MessageModel.id, MessageModel.role, MessageModel.text)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at)
            )
        ).all()
        for m_id, role, text in rows:
            if role == "user" and text == user_text:
                user_message_id = str(m_id)
            elif role == "assistant" and text == result.final_answer:
                assistant_message_id = str(m_id)
    resp = {
        "answer": result.final_answer,
        "messages": result.messages,
        "session_id": str(session_id),
        "user_id": str(user_id),
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
    }
    if guest_token:
        resp["guest_token"] = guest_token
    if notice:
        resp["notice"] = notice
    return resp


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    user: AuthUser | None = Depends(require_user_optional),
    queue: TaskQueue = Depends(get_task_queue),
    drive: DriveService = Depends(get_drive_service),
):
    """SSE streaming chat over the full agent path (tools + session persistence + quota).

    Emits ``{"type": "thinking"|"content"|"tool", "data": ...}`` deltas as the model reasons
    and answers, then a final ``{"type": "done", "data": {answer, session_id, user_id,
    user_message_id, assistant_message_id, notice}}`` event. A user with no usable LLM key
    degrades to the anonymous tier (guest quota), matching ``/chat``.
    """
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
    # All keys + the anonymous tier are exhausted too — block, don't fall back to the
    # legacy global connection; the user must top up / upgrade.
    if not base_url and not api_key:
        raise HTTPException(
            status_code=503,
            detail="当前没有可用的 LLM 渠道,无法使用聊天。请联系管理员配置渠道,或充值/升级套餐后重试。",
        )

    user_text = body.message
    if body.attach:
        note = await _attach_note(body, drive, user_id)
        if note:
            user_text = f"{note}\n\n{body.message}"
    session_id = body.session_id or await create_session(SessionLocal, user_id, title=body.message)
    session_memory = SessionMemoryStore(SessionLocal, _embedder(), llm, session_id, user_id)
    history = body.history or await session_memory.load_messages()
    history = await compact_history(
        history,
        session_factory=SessionLocal,
        session_id=session_id,
        session_memory=session_memory,
        llm=llm,
        model=model,
        base_url=base_url or None,
        api_key=api_key or None,
    )

    async def gen():
        # The agent may block on a human-in-the-loop approval (awaiting POST /approvals/{id}),
        # so a plain `async for` over run_stream would deadlock — the stream can't advance while
        # the approval-request frame sits unyielded. Pump the stream into a queue in a sibling
        # task, and let the ApprovalStore's sink push approval frames into the same queue; this
        # generator only ever reads from the queue.
        frames: asyncio.Queue = asyncio.Queue()
        store = ApprovalStore(
            get_approval_bridge().broker,
            user_id=str(user_id),
            sink=lambda evt: frames.put_nowait(("approval", evt)),
        )
        set_request_approval(store)

        async def pump():
            # NOTE: only ever await INSIDE run_stream (never on frames). If the pump task
            # suspended on `await frames.put` were cancelled there, the CancelledError would
            # be consumed by this `finally` and the run_stream generator abandoned — its
            # cleanup would only run on a later GC aclose (GeneratorExit, not CancelledError),
            # so `turn-cancelled` would never be logged. The queue is unbounded, so put_nowait
            # never blocks; a client disconnect then lands the CancelledError in the loop's
            # `except asyncio.CancelledError`, which logs turn-cancelled and closes memory.
            try:
                async for evt in get_agent().run_stream(
                    user_text,
                    history,
                    session_memory=session_memory,
                    model=model,
                    base_url=base_url or None,
                    api_key=api_key or None,
                    progress_sink=lambda evt: frames.put_nowait(("agent", evt)),
                ):
                    frames.put_nowait(("agent", evt))
            finally:
                # Sentinel so the consumer below always terminates after the stream ends,
                # including on cancellation (the loop already logs turn-cancelled).
                frames.put_nowait(("agent", {"type": "done", "data": None}))

        pump_task = asyncio.create_task(pump())
        final = None
        try:
            while True:
                kind, data = await frames.get()
                if kind == "approval":
                    # Approval-request frame: forward verbatim (client POSTs /approvals/{id}).
                    yield {"data": json.dumps(data, ensure_ascii=False, default=str)}
                    continue
                if data["type"] == "done":
                    final = data["data"]
                    break
                yield {"data": json.dumps(data, ensure_ascii=False)}
        finally:
            # Client disconnect / generator close: stop the pump so the turn's awaits
            # (wait_for/tenacity/gather) re-raise CancelledError and unwind cleanly.
            set_request_approval(None)
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task

        # Defer the expensive embed+summary work exactly like /chat; log usage now that the
        # turn's token counts are known.
        await queue.enqueue(SESSION_FINALIZE, {"session_id": str(session_id)})
        if log_user is not None:
            await _log_usage(
                log_user, business_name, "chat_stream",
                final["usage"] if final else None,
                credential_id=credential_id, paid=(tier == "paid"),
            )
        # Resolve this turn's user/assistant message ids (same scan as /chat: ascending,
        # last row matching each text) so the client can delete a single message.
        answer = (final or {}).get("answer", "")
        user_message_id = assistant_message_id = None
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(MessageModel.id, MessageModel.role, MessageModel.text)
                    .where(MessageModel.session_id == session_id)
                    .order_by(MessageModel.created_at)
                )
            ).all()
            for m_id, role, text in rows:
                if role == "user" and text == user_text:
                    user_message_id = str(m_id)
                elif role == "assistant" and answer and text == answer:
                    assistant_message_id = str(m_id)
        done = {
            "answer": answer,
            "session_id": str(session_id),
            "user_id": str(user_id),
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
        }
        if guest_token:
            done["guest_token"] = guest_token
        if notice:
            done["notice"] = notice
        yield {"data": json.dumps({"type": "done", "data": done}, ensure_ascii=False)}

    return EventSourceResponse(gen())
