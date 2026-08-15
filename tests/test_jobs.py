"""Tests for JobStore/TaskQueue using a fake session + fake redis (no real DB).

Verifies the single-source-of-truth contract: enqueue creates a PostgreSQL job row and hands
the string job id to arq; status transitions are driven through the JobStore; and get() reads
the terminal state back.
"""
import uuid

from core.infrastructure.jobs import (
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    JobStore,
    TaskQueue,
)


class _FakeSession:
    def __init__(self, store):
        self._store = store

    def add(self, obj):
        self._store.rows.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        if obj.id is None:
            obj.id = uuid.uuid4()

    async def get(self, model, ident):
        for r in self._store.rows:
            if getattr(r, "id", None) == ident:
                return r
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Store:
    def __init__(self):
        self.rows = []

    def session(self):
        return _FakeSession(self)


class _FakeRedis:
    def __init__(self):
        self.enqueued = []

    async def enqueue_job(self, name, job_id, payload):
        self.enqueued.append((name, job_id, payload))


async def test_job_store_lifecycle():
    store = _Store()
    jobs = JobStore(store.session)

    job = await jobs.create("tts", {"text": "hi"})
    assert job.status == QUEUED
    assert job.id is not None

    assert (await jobs.get(job.id)).status == QUEUED

    await jobs.mark_running(job.id)
    assert (await jobs.get(job.id)).status == RUNNING

    await jobs.mark_succeeded(job.id, {"url": "/audio/x.mp3"})
    done = await jobs.get(job.id)
    assert done.status == SUCCEEDED
    assert done.result == {"url": "/audio/x.mp3"}


async def test_task_queue_enqueue_and_get():
    store = _Store()
    redis = _FakeRedis()
    queue = TaskQueue(redis, JobStore(store.session))

    jid = await queue.enqueue("tts", {"text": "hello"})

    assert redis.enqueued == [("tts", str(jid), {"text": "hello"})]

    state = await queue.get(jid)
    assert state["status"] == QUEUED
    assert state["result"] is None

    await queue.job_store.mark_succeeded(jid, {"url": "/audio/hello.mp3"})
    state = await queue.get(jid)
    assert state["status"] == SUCCEEDED
    assert state["result"] == {"url": "/audio/hello.mp3"}


async def test_task_queue_reports_failure():
    store = _Store()
    queue = TaskQueue(_FakeRedis(), JobStore(store.session))

    jid = await queue.enqueue("tts", {})
    await queue.job_store.mark_failed(jid, "boom")

    state = await queue.get(jid)
    assert state["status"] == FAILED
    assert state["error"] == "boom"


async def test_task_queue_unknown_job():
    queue = TaskQueue(_FakeRedis(), JobStore(_Store().session))

    state = await queue.get(uuid.uuid4())
    assert state["status"] == "unknown"
