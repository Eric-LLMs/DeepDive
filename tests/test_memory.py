"""Tests for SessionMemoryStore using a fake session factory (no real DB).

Covers the hot-path contract: append_message writes text only (embedding backfilled later),
close flushes buffered events and clears the buffer, and search maps recalled rows into the
duck-typed dict shape the loop consumes.
"""
import uuid

from core.infrastructure.db import MessageModel, SessionEventModel
from core.infrastructure.memory import SessionMemoryStore


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
