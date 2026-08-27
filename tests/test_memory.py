"""Tests for SessionMemoryStore using a fake session factory (no real DB).

Covers the hot-path contract: append_message writes text only (embedding backfilled later),
close flushes buffered events and clears the buffer, and search maps recalled rows into the
duck-typed dict shape the loop consumes.
"""
import uuid
from types import SimpleNamespace

from core.infrastructure.db import MessageModel, SessionEventModel
from core.infrastructure.memory import SessionMemoryStore, load_session_detail


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, store):
        self._store = store

    def add(self, obj):
        self._store.added.append(obj)

    async def commit(self):
        pass

    async def execute(self, stmt):
        return _Result(self._store.search_rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Store:
    def __init__(self):
        self.added = []
        self.search_rows = []

    def session(self):
        return _FakeSession(self)


def _mem(store):
    return SessionMemoryStore(
        store.session, embedder=None, llm=None, session_id=uuid.uuid4(), user_id=uuid.uuid4()
    )


async def test_append_message_writes_text_only():
    store = _Store()
    mem = _mem(store)

    await mem.append_message("user", "hello world")

    assert len(store.added) == 1
    msg = store.added[0]
    assert isinstance(msg, MessageModel)
    assert msg.role == "user"
    assert msg.text == "hello world"
    assert msg.embedding is None  # embedding is backfilled by session_finalize, not here


async def test_close_flushes_events_and_clears():
    store = _Store()
    mem = _mem(store)
    mem.record_event("session-start", {"user_msg": "hi"})
    mem.record_event("step-end", {})

    await mem.close()

    assert len(store.added) == 2
    assert all(isinstance(e, SessionEventModel) for e in store.added)
    assert mem._events == []


async def test_search_maps_recalled_rows():
    store = _Store()
    mem = _mem(store)
    m = MessageModel(role="assistant", text="remember this")
    m.id = uuid.uuid4()
    store.search_rows = [(m, 0.9)]

    results = await mem.search([0.1, 0.2], top_k=5)

    assert results == [
        {"id": str(m.id), "role": "assistant", "text": "remember this", "score": 0.9}
    ]


class _DetailResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return _DetailScalars(self._value)


class _DetailScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _DetailSession:
    def __init__(self, sess, msgs):
        self._calls = [sess, msgs]  # load_session_detail: session row, then messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return _DetailResult(self._calls.pop(0))


async def test_load_session_detail_carries_imported_rag_flag():
    mid1, mid2 = uuid.uuid4(), uuid.uuid4()
    sess = SimpleNamespace(title="Chat", imported_rag=None)  # title read only
    msgs = [
        SimpleNamespace(
            id=mid1, role="user", text="Q", imported_rag=True,
            created_at=SimpleNamespace(isoformat=lambda: "2026-08-28T10:00:00+00:00"),
        ),
        SimpleNamespace(
            id=mid2, role="assistant", text="A", imported_rag=False,
            created_at=SimpleNamespace(isoformat=lambda: "2026-08-28T10:00:01+00:00"),
        ),
    ]
    detail = await load_session_detail(lambda: _DetailSession(sess, msgs), uuid.uuid4())

    assert detail["messages"][0]["imported_rag"] is True
    assert detail["messages"][1]["imported_rag"] is False
    assert detail["messages"][0]["id"] == str(mid1)
