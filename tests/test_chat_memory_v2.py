"""Chat session-memory v2: client Live State authority + zero-read hot path.

Covers the plan's v2 invariants at the router-assembly layer (``_assemble_turn_history``),
the schema guards (§8.3 empty-tail), the write queue / done-frame ids, reconcile
alignment, ownership, and the worker recovery+compaction path — with the shared
hand-rolled fakes (no real DB).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from api.routers import chat as chat_mod
from api.routers import sessions as sessions_mod
from api.schemas import (
    ChatContextState,
    ChatRequest,
    ChatTurnMessage,
    ReconcileRequest,
)
from core.infrastructure import memory
from core.infrastructure.memory import (
    SessionMemoryStore,
    reconcile_messages,
)
from fastapi import HTTPException
from pydantic import ValidationError

from tests._memory_v2_fakes import (
    Db,
    EventsStore,
    Llm,
    factory,
    make_rows,
)

SID = uuid4()


def _v2_body(tail_rows, *, summary=None, through=None, pending=False, message="new question"):
    tail = [
        ChatTurnMessage(message_id=r.id, role=r.role, content=r.text) for r in tail_rows
    ]
    return ChatRequest(
        message=message,
        session_id=SID,
        tail=tail,
        context_state=ChatContextState(
            summary=summary, through_message_id=through, has_pending_mutations=pending
        ),
    )


def _patch(monkeypatch, db, llm):
    monkeypatch.setattr(chat_mod, "SessionLocal", factory(db))
    monkeypatch.setattr(chat_mod, "llm", llm)


async def _assemble(body, db, llm, monkeypatch, user_text="new question"):
    _patch(monkeypatch, db, llm)
    store = EventsStore()
    history, compaction, deferred = await chat_mod._assemble_turn_history(
        body, store, SID, user_text, model=None, base_url=None, api_key=None,
    )
    return history, compaction, deferred, store


# ── items 1/2/3/4/5: normal v2 turn — zero reads, client content authoritative ──

async def test_normal_v2_turn_reads_no_messages_or_sessions(monkeypatch):
    rows = make_rows(10)
    db = Db(rows=rows)
    llm = Llm()
    history, compaction, deferred, _ = await _assemble(_v2_body(rows), db, llm, monkeypatch)
    assert compaction is None and deferred is None
    assert [m["content"] for m in history] == [r.text for r in rows]
    assert db.messages_selects == 0 and db.sessions_selects == 0
    assert llm.calls == []  # 0 extra compaction LLM calls below threshold


async def test_client_summary_and_tail_assemble_the_prompt(monkeypatch):
    rows = make_rows(8)
    db = Db(rows=rows)
    history, *_ = await _assemble(
        _v2_body(rows, summary="CLIENT SUMMARY"), db, Llm(), monkeypatch
    )
    assert history[0] == {
        "role": "system", "content": "## Conversation summary\nCLIENT SUMMARY"
    }
    assert db.messages_selects == 0


async def test_client_edited_content_wins_over_sql(monkeypatch):
    rows = make_rows(6)
    # SQL holds the OLD text; the client tail was edited locally (edit=re-ask path).
    body = _v2_body(rows)
    body.tail[1] = ChatTurnMessage(message_id=rows[1].id, role="assistant", content="EDITED")
    db = Db(rows=rows)
    history, *_ = await _assemble(body, db, Llm(), monkeypatch)
    assert history[1]["content"] == "EDITED"
    assert rows[1].text not in [m["content"] for m in history]


# ── item 1c: §8.3 no silent fallback — invalid v2 payloads are hard errors ───

def test_empty_tail_with_watermark_is_rejected():
    with pytest.raises(ValidationError):
        _v2_body([], through=uuid4())
    with pytest.raises(ValidationError):
        _v2_body([], summary="some summary")


def test_fresh_v2_session_empty_tail_is_legal():
    body = _v2_body([])  # summary=None, through=None
    assert body.context_state is not None and body.tail == []


def test_role_whitelist_enforced():
    with pytest.raises(ValidationError):
        ChatTurnMessage(message_id=None, role="system", content="x")


# ── items 6/7/8: threshold → fold; failure paths never trim ──────────────────

async def test_over_threshold_compacts_and_returns_payload(monkeypatch):
    rows = make_rows(45)
    db = Db(rows=rows)
    llm = Llm()
    history, compaction, deferred, store = await _assemble(_v2_body(rows), db, llm, monkeypatch)
    assert deferred is None and compaction is not None
    assert compaction["through_message_id"] == str(rows[-1].id)  # INCLUSIVE
    assert history[0]["content"] == "## Conversation summary\nFAKE SUMMARY"
    assert [m["content"] for m in history[1:]] == [r.text for r in rows[25:]]
    assert len(llm.calls) == 1  # ONE fold; overflow (25) folded from raw rows
    assert "msg 0" in llm.prompts[0]
    assert [t for t, _ in store.events] == ["compaction"]


async def test_turn_n_plus_one_reads_zero_after_compaction(monkeypatch):
    rows = make_rows(45)
    db = Db(rows=rows)
    llm = Llm()
    _history, compaction, _d, _s = await _assemble(_v2_body(rows), db, llm, monkeypatch)
    # Client applies the done frame: Live State = new summary + the kept tail (own ids).
    db.reset_reads()
    next_body = _v2_body(
        rows[25:], summary=compaction["summary"],
        through=UUID(compaction["through_message_id"]),
    )
    _history2, compaction2, deferred2, _ = await _assemble(next_body, db, llm, monkeypatch)
    assert compaction2 is None and deferred2 is None
    assert db.messages_selects == 0 and db.sessions_selects == 0  # §8.5
    assert len(llm.calls) == 1  # no re-fold on turn N+1


async def test_client_pending_mutations_defers_without_reads(monkeypatch):
    rows = make_rows(45)
    db = Db(rows=rows)
    llm = Llm()
    history, compaction, deferred, _ = await _assemble(
        _v2_body(rows, pending=True), db, llm, monkeypatch
    )
    assert compaction is None
    assert deferred == "client_pending_mutations"
    assert len(history) == 45  # over-budget tail used as-is, nothing trimmed
    assert db.messages_selects == 0 and db.sessions_selects == 0
    assert llm.calls == []


async def test_server_flush_failure_defers_before_fold(monkeypatch):
    rows = make_rows(45)
    db = Db(rows=rows)
    llm = Llm()

    async def boom(*a, **k):
        raise RuntimeError("queue flush failed")

    monkeypatch.setattr(memory, "flush_session_writes", boom)
    history, compaction, deferred, _ = await _assemble(_v2_body(rows), db, llm, monkeypatch)
    assert deferred == "persist_barrier_failed"
    assert compaction is None and llm.calls == [] and db.saved_payloads == []
    assert len(history) == 45


async def test_fold_llm_failure_returns_full_context(monkeypatch):
    rows = make_rows(45)
    db = Db(rows=rows)
    llm = Llm(fail=True)
    history, compaction, deferred, _ = await _assemble(_v2_body(rows), db, llm, monkeypatch)
    assert deferred == "fold_failed"
    assert compaction is None and db.saved_payloads == []
    assert [m["content"] for m in history] == [r.text for r in rows]  # nothing dropped


# ── items 11/14: recovery mode (legacy client + worker) ──────────────────────

async def test_legacy_client_uses_bounded_recovery_load(monkeypatch):
    rows = make_rows(5)
    db = Db(rows=rows)
    body = ChatRequest(message="hi", session_id=SID)  # no context_state → legacy
    history, _compaction, _deferred, _ = await _assemble(body, db, Llm(), monkeypatch)
    assert history == [{"role": r.role, "content": r.text} for r in rows]
    assert db.messages_selects >= 1  # recovery reads SQL — the ONLY legacy mode that does


async def test_recovery_from_checkpoint_then_zero_read(monkeypatch):
    rows = make_rows(45)
    ck = {
        "revision": 2, "through_message_id": str(rows[24].id),
        "through_created_at": rows[24].created_at.isoformat(),
        "summary": "CHECKPOINT SUMMARY", "summary_chars": 18,
        "last_compaction_at": None, "fold_count": 1,
    }
    db = Db(rows=rows[25:], checkpoint=ck)
    body = ChatRequest(message="hi", session_id=SID)  # legacy/recovery entry
    history, *_ = await _assemble(body, db, Llm(), monkeypatch)
    assert history[0]["content"] == "## Conversation summary\nCHECKPOINT SUMMARY"
    assert [m["content"] for m in history[1:]] == [r.text for r in rows[25:]]
    # A client that recovered then goes v2: next turn is zero-read.
    db.reset_reads()
    next_body = _v2_body(rows[25:], summary="CHECKPOINT SUMMARY", through=rows[24].id)
    _h2, c2, d2, _ = await _assemble(next_body, db, Llm(), monkeypatch)
    assert c2 is None and d2 is None
    assert db.messages_selects == 0 and db.sessions_selects == 0


# ── item 12: async write queue — retry, persist_failed, ids for the done frame ──

async def test_write_queue_batches_retries_and_surfaces_persist_failed():
    sid = uuid4()  # unique per test: the queue is keyed by session
    db = Db(insert_fail=True, insert_n=2, insert_roles=["user", "assistant"])
    store = SessionMemoryStore(factory(db), None, None, sid, uuid4())
    await store.append_message("user", "hello")   # enqueue only — no INSERT yet
    assert db.inserts == []

    rows = await store.flush_writes()
    assert rows == [] and store.persist_failed is True
    assert store.take_written() == []

    db.insert_fail = False
    rows = await store.flush_writes()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    written = store.take_written()
    assert [w["message_id"] for w in written] == [r["message_id"] for r in rows]
    assert db.inserts == [2]  # ONE batch INSERT for the whole turn
    # ids exist for the done frame without any text scan
    assert chat_mod._last_written_id(written, "user") == written[0]["message_id"]
    assert chat_mod._last_written_id(written, "assistant") == written[1]["message_id"]
    assert chat_mod._last_written_id(written, "tool") is None


# ── item 12/13: reconcile aligns SQL to the client (client authoritative) ────

async def test_reconcile_deletes_inserts_and_rewrites_with_null_embedding():
    sid, uid = uuid4(), uuid4()
    r0, r1, r2 = make_rows(3)
    r0.embedding = [0.5]
    r1.embedding = [0.5]
    db = Db(rows=[r0, r1, r2], boundary=None)
    tail = [
        {"message_id": str(r0.id), "role": r0.role, "content": r0.text},   # match
        {"message_id": str(r1.id), "role": r1.role, "content": "client fix"},  # mismatch
        {"message_id": None, "role": "user", "content": "never persisted"},     # insert
        {"message_id": str(uuid4()), "role": "user", "content": "foreign id"},  # rejected id
    ]
    counts = await reconcile_messages(factory(db), uid, sid, tail)
    assert counts == {"deleted": 1, "inserted": 2, "updated": 1}
    assert r1.text == "client fix" and r1.embedding is None       # re-embed queued
    assert r0.embedding == [0.5]
    assert [d.id for d in db.deleted] == [r2.id]                  # client-missing row gone
    assert {a.role for a in db.added} == {"user"}                 # inserts
    assert len(db.added) == 2


async def test_reconcile_route_rejects_foreign_session(monkeypatch):
    owner, caller = uuid4(), uuid4()
    db = Db(session_row=SimpleNamespace(id=owner, user_id=owner))
    monkeypatch.setattr(sessions_mod, "SessionLocal", factory(db))
    body = ReconcileRequest(context_state=ChatContextState(), tail=[])
    with pytest.raises(HTTPException) as exc:
        await sessions_mod.reconcile_session(uuid4(), body, user=SimpleNamespace(user_id=caller))
    assert exc.value.status_code == 404


# ── item 14: worker chat_turn gets the recovery+compaction path ──────────────

async def test_worker_run_agent_turn_compacts_when_over_threshold(monkeypatch):
    from apps.worker import tasks

    class _FakeStore:
        def __init__(self, *args):
            self.events = []

        def record_event(self, type_, payload):
            self.events.append((type_, payload))

        async def close(self):
            pass

    class _Kernel:
        def __init__(self):
            self.histories = []
            self.stores = []

        async def run(self, message, history, **kwargs):
            self.histories.append(history)
            self.stores.append(kwargs["session_memory"])
            return SimpleNamespace(
                final_answer="done", messages=[{"role": "assistant"}],
                usage={}, cost_usd=0.0,
            )

    user_id = uuid4()
    rows = make_rows(45)
    db = Db(rows=rows)
    llm = Llm()
    kernel = _Kernel()
    monkeypatch.setattr(tasks, "get_agent_kernel", lambda: kernel)
    monkeypatch.setattr(tasks, "SessionMemoryStore", _FakeStore)
    ctx = {
        "job_store": SimpleNamespace(
            create=AsyncMock(return_value=SimpleNamespace(id=uuid4())),
            mark_running=AsyncMock(),
            mark_succeeded=AsyncMock(),
            mark_failed=AsyncMock(),
            get=AsyncMock(return_value=SimpleNamespace(user_id=None)),
        ),
        "redis": _FakeRedis(),
        "session_factory": factory(db),
        "embedder": None,
        "llm": llm,
        "job_try": 1,
    }

    await tasks.run_agent_turn(
        ctx, str(uuid4()),
        {"user_id": str(user_id), "session_id": str(SID), "message": "continue"},
    )

    history = kernel.histories[0]
    assert history[0]["content"] == "## Conversation summary\nFAKE SUMMARY"  # compacted
    assert len(llm.calls) == 1                       # one fold on the worker too
    assert [t for t, _ in kernel.stores[0].events] == ["compaction"]  # audited
    assert ("session_finalize", {"session_id": str(SID)})[0] in [
        e[0] for e in ctx["redis"].enqueued
    ]


class _FakeRedis:
    def __init__(self):
        self.enqueued = []

    async def enqueue_job(self, type_, job_id, payload):
        self.enqueued.append((type_, payload))
