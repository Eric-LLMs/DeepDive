"""Turn lifecycle: post-execution bookkeeping (persistence, usage, research hand-off).

Nothing here runs before execution; it is what the legacy router's ``finally`` /
``finalize_turn`` blocks did, verbatim — session-finalize enqueue, usage metering,
message-id resolution from the write queue, the research mirror + worker hand-off
(T4 invariant #3: a client disconnect never strands a run slot), the retrieval
snapshot and the viewer post-turn persistence.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from uuid import UUID

from core.application.chat.context import ChatTurnContext
from core.application.chat.executors.base import ChatDeps
from core.infrastructure.db import MessageModel, SessionLocal
from core.infrastructure.jobs import RESEARCH_DRIVE, SESSION_FINALIZE

logger = logging.getLogger(__name__)


def last_written_id(rows: list[dict], role: str) -> str | None:
    """Id of the LAST written row with ``role`` (this turn's message), or None."""
    for r in reversed(rows):
        if r["role"] == role:
            return r["message_id"]
    return None


async def persist_turn_meta(session_factory, message_id: str | None, key: str, value) -> None:
    """Attach one JSONB key to a message row (``retrieval`` / ``viewer`` / …). Best-effort:
    a failure only means that row's metadata won't survive a reopen, never fails the turn.
    The row may still be in-flight in the session write queue, so retry briefly on a
    missing row."""
    if not message_id:
        return
    for attempt in range(3):
        try:
            async with session_factory() as s:
                row = await s.get(MessageModel, UUID(message_id))
                if row is None:
                    raise RuntimeError("message row not visible yet")
                meta = dict(row.meta or {})
                meta[key] = value
                row.meta = meta
                await s.commit()
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("meta %s persist failed (%d/3): %s", key, attempt + 1, exc)
            await asyncio.sleep(0.5)


def extract_retrieval(messages: list[dict] | None) -> dict | None:
    """Snapshot this turn's rag_search hits for the retrieval-feedback loop.

    Maps assistant ``tool_calls`` (flat {id, name, arguments} shape) so each tool-role row
    is identified by name; parses every ``rag_search`` result (a JSON list of hit dicts)
    into a compact ``{"hits": [{id, score, text<=200}], "queries": [...]}``.
    ``_UNAVAILABLE`` / malformed payloads contribute nothing; hits dedupe by id keeping the
    first score. Returns None when the turn ran no successful rag_search.
    """
    if not messages:
        return None
    name_by_call: dict[str, str] = {}
    query_by_call: dict[str, str] = {}
    for m in messages:
        for tc in m.get("tool_calls") or []:
            tcid, name = tc.get("id"), tc.get("name")
            if not tcid or not name:
                continue
            name_by_call[tcid] = name
            if name == "rag_search":
                try:
                    q = (json.loads(tc.get("arguments") or "{}") or {}).get("query")
                except Exception:  # noqa: BLE001 - malformed args just means no query tag
                    q = None
                if q:
                    query_by_call[tcid] = str(q)
    hits: dict[str, dict] = {}
    queries: list[str] = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        cid = m.get("tool_call_id")
        if name_by_call.get(cid) != "rag_search":
            continue
        content = m.get("content")
        if isinstance(content, list):  # text-block parts shape
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        try:
            parsed = json.loads(content) if isinstance(content, str) else None
        except Exception:  # noqa: BLE001
            parsed = None
        if not isinstance(parsed, list):
            continue
        if cid in query_by_call and query_by_call[cid] not in queries:
            queries.append(query_by_call[cid])
        for h in parsed:
            if not isinstance(h, dict):
                continue
            hid = h.get("id") or h.get("chunk_id")
            if hid is None or str(hid) in hits:
                continue
            score = h.get("score")
            hits[str(hid)] = {
                "id": str(hid),
                "score": float(score) if isinstance(score, (int, float)) else None,
                "text": str(h.get("text") or "")[:200],
            }
    if not hits:
        return None
    return {"hits": list(hits.values()), "queries": queries}


def _viewer_stub_reads(messages: list[dict] | None, stub_asset_id: str) -> list[dict]:
    """Every ``read_document`` call this turn made against the stub's asset.

    Enumerated from the assistant ``tool_calls`` (flat {id, name, arguments} shape), so
    FAILED calls are recorded too — the trace documents what the model tried, not only
    what succeeded. ``pages`` is the actual argument (None = full-document read).
    """
    reads: list[dict] = []
    for m in messages or []:
        for tc in m.get("tool_calls") or []:
            if tc.get("name") != "read_document":
                continue
            args = tc.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:  # noqa: BLE001 - malformed args: asset match untestable
                    args = {}
            args = args if isinstance(args, dict) else {}
            if str(args.get("asset_id") or "") != str(stub_asset_id):
                continue
            reads.append({"tool_call_id": tc.get("id"), "pages": args.get("pages")})
    return reads


async def viewer_post_turn(
    deps: ChatDeps, assembly: dict | None, body, answer: str,
    messages: list[dict] | None,
    user_message_id: str | None, assistant_message_id: str | None,
) -> dict | None:
    """Persist the sent-time snapshot (user row) + citation map/validation (assistant
    row) — both under dedicated ``meta`` keys, fully separate from ``meta["retrieval"]``.

    ``status=="stub"`` instead traces the tool-driven read: the Viewer Access Context
    section told the model to call ``read_document``, and we record which calls it actually
    made (incl. failures) against that asset on the assistant row."""
    if not assembly:
        return None
    if assembly["status"] == "stub":
        stub = assembly.get("stub") or {}
        reads = _viewer_stub_reads(messages, stub.get("asset_id"))
        payload = {
            "mode": assembly["mode"], "status": "stub",
            "asset_id": stub.get("asset_id"), "current_page": stub.get("page"),
            "reads": reads,
        }
        if assistant_message_id:
            await deps.persist_turn_meta(assistant_message_id, "viewer", payload)
        return payload
    if assembly["status"] != "injected":
        return None
    blocks = assembly["blocks"]
    if user_message_id:
        await deps.persist_turn_meta(
            user_message_id, "viewer", deps.viewer.snapshot(body.viewer, blocks)
        )
    cited, invalid = deps.viewer.validate_citations(answer or "", blocks)
    if assistant_message_id:
        await deps.persist_turn_meta(assistant_message_id, "viewer_citations", {
            "map": deps.viewer.citation_map(blocks), "cited": cited, "invalid": invalid,
        })
    return {
        "mode": assembly["mode"], "status": assembly["status"],
        "citations": deps.viewer.citation_map(blocks), "cited": cited, "invalid": invalid,
        "rejected": assembly["rejected"],
    }


async def finalize_turn(
    ctx: ChatTurnContext, deps: ChatDeps, final_payload: dict | None, *, tool: str
) -> dict:
    """Post-run bookkeeping shared by both routes and by the research disconnect path:
    session-finalize enqueue, usage logging, message-id resolution, and the task's
    ``session_history.json`` mirror. The DB SessionModel stays the authoritative chat
    record; the mirror is a task-local projection and failures only log."""
    body = ctx.body
    await deps.queue.enqueue(SESSION_FINALIZE, {"session_id": str(ctx.session_id)}, user_id=ctx.user_id)
    if ctx.log_user is not None:
        await deps.log_usage(
            ctx.log_user, ctx.business_name, tool,
            final_payload["usage"] if final_payload else None,
            credential_id=ctx.credential_id, paid=(ctx.tier == "paid"),
        )
    # Resolve this turn's message ids from the write queue's RETURNING rows
    # (run's close() already flushed them into the buffer). On persistence failure the
    # ids are simply null and ``persist_failed`` tells the client its Live State still
    # owns the transcript.
    answer = (final_payload or {}).get("answer", "")
    await ctx.session_memory.flush_writes()
    written = ctx.session_memory.take_written()
    user_message_id = last_written_id(written, "user")
    assistant_message_id = last_written_id(written, "assistant")
    if ctx.research_service is not None and ctx.bound_task_id is not None:
        # Mirror this turn into the bound task's session_history.json (best-effort).
        try:
            await ctx.research_service.append_session_turn(ctx.user_id, ctx.session_id, "user", body.message)
            if answer:
                await ctx.research_service.append_session_turn(
                    ctx.user_id, ctx.session_id, "assistant", answer
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("research session_history mirror failed: %s", exc)
    done = {
        "answer": answer,
        "session_id": str(ctx.session_id),
        "user_id": str(ctx.user_id),
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
    }
    # Retrieval-feedback snapshot: persist onto the assistant row + hand the client the
    # hits to rate (POST /rag/feedback records the rating).
    retrieval = extract_retrieval((final_payload or {}).get("messages"))
    if retrieval:
        await deps.persist_turn_meta(assistant_message_id, "retrieval", retrieval)
        done["retrieved"] = retrieval
    viewer_payload = await viewer_post_turn(
        deps, ctx.viewer_assembly, body, answer, (final_payload or {}).get("messages"),
        user_message_id, assistant_message_id
    )
    if viewer_payload:
        done["viewer"] = viewer_payload
    if ctx.compaction_payload:
        done["compaction"] = ctx.compaction_payload
    if ctx.compaction_deferred:
        done["compaction_deferred"] = ctx.compaction_deferred
    if ctx.session_memory.persist_failed:
        done["persist_failed"] = True
    if ctx.guest_token:
        done["guest_token"] = ctx.guest_token
    if ctx.research_notice:
        ctx.notice = f"{ctx.notice}\n{ctx.research_notice}" if ctx.notice else ctx.research_notice
    if ctx.notice:
        done["notice"] = ctx.notice
    return done


async def maybe_continue_research(
    service,
    queue,
    *,
    user_id,
    task_id: str,
    run_id: str,
    session_id: str | None,
) -> bool:
    """Hand an interactive research turn's run to the worker chain, or release it.

    Called right after the first (interactive) turn of a run completes, *before* ``end_run``.
    Returns ``True`` when the run was handed to ``RESEARCH_DRIVE`` (the slot stays live and
    the worker keeps driving until PUBLISH / a gate / a stop); ``False`` when the run must be
    released here (reached PUBLISH, a human gate override is pending, Stop was requested, or
    the continuation could not be scheduled — the slot is never stranded).

    The interactive turn is the "free" turn 0: the driver's no-progress / caps / cost grading
    starts with auto-turn 1. The payload carries NO LLM credentials — each worker turn
    resolves the owner's channel through the dispatch gateway at its own job start.
    """
    from plugins.research.driver import ResearchRunDriver, iso_now  # lazy: cycle guard

    async def _publish_async(kind: str) -> None:
        with contextlib.suppress(Exception):
            await service.publish_change(user_id, task_id, kind=kind)

    async def _release_and_publish(kind: str) -> None:
        # Release the active-run slot BEFORE publishing the terminal event. The caller
        # runs ``end_run`` only after this returns, so a monitor refetch triggered by the
        # event could otherwise observe a still-RUNNING slot (the pop hadn't committed),
        # re-green the desktop Run button, and then never be corrected — end_run's own
        # revision bump publishes no event. Popping first guarantees any refetch sees
        # ``is_running=false``. end_run is idempotent, so the caller's later end_run no-ops.
        try:
            service.end_run(user_id, task_id)
        except Exception as exc:  # noqa: BLE001 - the caller still attempts the release
            logger.warning("research pre-event slot release failed: %s", exc)
        await _publish_async(kind)

    ledger = service.get_driver_checkpoint(user_id, task_id)
    if ledger.get("cancel_requested"):
        await _release_and_publish("run.cancelled")
        return False
    project = service.read_project(user_id, task_id)
    if project.get("stage") == "PUBLISH":
        await _release_and_publish("run.finished")
        return False
    if service.pending_overrides(user_id, task_id):
        # A run parked on a gate must explain itself in the task chat first: write the
        # deterministic review note to the session DB *before* the blocked wake-up is
        # published, so a monitor refetch observes the note next to the Approve/Reject card.
        if session_id:
            try:
                await service.emit_gate_notes(SessionLocal, user_id, task_id, session_id)
            except Exception as exc:  # noqa: BLE001 - never fail the parking decision
                logger.warning("research gate note emission failed: %s", exc)
        await _release_and_publish("run.blocked")
        return False

    # Persist the interactive turn (turn 0) as the chain's starting ledger, then schedule
    # auto-turn 1. The driver CAS-checks on arrival; a duplicate run of turn 0 is impossible
    # (this is the only site that schedules turn_index 1).
    try:
        service.set_driver_checkpoint(
            user_id, task_id,
            patch={
                "run_id": run_id,
                "turn_index": 0,
                "turn_attempt": 1,
                "turn_state": "done",
                "execution_id": f"{run_id}:0:1",
                "updated_at": iso_now(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - treat as a schedule failure below
        logger.warning("research continuation ledger failed: %s", exc)
        ResearchRunDriver().abort_run(
            service, user_id, task_id,
            run_id=run_id, execution_id=f"{run_id}:0:1",
            reason=f"could not record the run ledger: {exc}",
        )
        return False

    try:
        await queue.enqueue(
            RESEARCH_DRIVE,
            {
                "user_id": str(user_id),
                "task_id": task_id,
                "run_id": run_id,
                "session_id": session_id,
                "turn_index": 1,
            },
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 - never strand a RUNNING slot
        logger.warning("research continuation enqueue failed: %s", exc)
        ResearchRunDriver().abort_run(
            service, user_id, task_id,
            run_id=run_id, execution_id=f"{run_id}:0:1",
            reason=f"could not schedule the first auto turn: {exc}",
        )
        return False

    await _publish_async("run.turn")
    return True


async def handle_research_post_turn(ctx: ChatTurnContext, deps: ChatDeps) -> bool:
    """The shared finally-block research policy: decide worker hand-off or slot release.

    Returns ``research_continuing``. Never raises — a continuation hiccup only logs.
    """
    if not (ctx.research_turn and ctx.research_service is not None and ctx.bound_task_id is not None):
        return False
    try:
        project = ctx.research_service.read_project(ctx.user_id, ctx.bound_task_id)
        active_run = project.get("active_run") or {}
        run_id = active_run.get("run_id")
        if run_id:
            continuing = await maybe_continue_research(
                ctx.research_service,
                deps.queue,
                user_id=ctx.user_id,
                task_id=ctx.bound_task_id,
                run_id=run_id,
                session_id=str(ctx.session_id),
            )
            if not continuing:
                try:
                    ctx.research_service.end_run(ctx.user_id, ctx.bound_task_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("research end_run failed: %s", exc)
            return continuing
    except Exception as exc:  # noqa: BLE001
        logger.warning("research continuation decision failed: %s", exc)
    # Fall-through: the slot is released unless ``maybe_continue_research`` already did it.
    try:
        ctx.research_service.end_run(ctx.user_id, ctx.bound_task_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("research end_run failed: %s", exc)
    return False
