"""Worker honest status: FAILED only on the final attempt; retrying notes otherwise.

``_run`` must never leave PG showing a false FAILED while arq is still retrying, and a
terminal failure records a dead-letter marker on the audit log.
"""
import asyncio
import json
import uuid

import pytest
from core.config import settings

from apps.worker import tasks


class _Job:
    def __init__(self, id):
        self.id = id
        self.status = None
        self.error = None


class _FakeSession:
    def __init__(self, store):
        self._store = store

    async def get(self, model, ident):
        for r in self._store.rows:
            if str(r.id) == str(ident):
                return r
        return None

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Store:
    def __init__(self):
        self.rows = []

    def session(self):
        return _FakeSession(self)


class _JobStore:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def _find(self, job_id):
        return next(r for r in self.store.rows if str(r.id) == str(job_id))

    async def mark_running(self, job_id, error=None):
        self.calls.append(("running", error))
        job = self._find(job_id)
        job.status = "running"
        job.error = error

    async def mark_succeeded(self, job_id, result):
        self.calls.append(("succeeded", result))

    async def mark_failed(self, job_id, error):
        self.calls.append(("failed", error))
        job = self._find(job_id)
        job.status = "failed"
        job.error = error


def _ctx(store, job_try):
    job = _Job(uuid.uuid4())
    store.rows.append(job)
    return {"job_store": _JobStore(store), "job_try": job_try}, job


def _raising_work():
    async def work():
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    return work()


def _ok_work(result):
    async def work():
        return result

    return work()


async def test_non_terminal_failure_keeps_running_with_retry_note(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worker_max_tries", 3)
    monkeypatch.setattr(settings, "audit_log_path", tmp_path / "audit.jsonl")
    store = _Store()
    ctx, job = _ctx(store, job_try=1)
    with pytest.raises(RuntimeError):
        await tasks._run(ctx, str(job.id), _raising_work())
    assert ctx["job_store"].calls[-1][0] == "running"
    assert "retrying" in ctx["job_store"].calls[-1][1]
    assert job.status == "running"
    # No dead-letter for a retryable failure.
    assert not (tmp_path / "audit.jsonl").exists()


async def test_terminal_failure_marks_failed_and_dead_letters(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worker_max_tries", 1)
    monkeypatch.setattr(settings, "audit_log_path", tmp_path / "audit.jsonl")
    store = _Store()
    ctx, job = _ctx(store, job_try=1)
    with pytest.raises(RuntimeError):
        await tasks._run(ctx, str(job.id), _raising_work())
    assert ctx["job_store"].calls[-1][0] == "failed"
    assert job.status == "failed"
    payload = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert payload["event"] == "job_dead_letter"
    assert payload["attempt"] == 1
    assert payload["error"] == "boom"


async def test_final_attempt_of_retry_budget_marks_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worker_max_tries", 3)
    monkeypatch.setattr(settings, "audit_log_path", tmp_path / "audit.jsonl")
    store = _Store()
    ctx, job = _ctx(store, job_try=3)
    with pytest.raises(RuntimeError):
        await tasks._run(ctx, str(job.id), _raising_work())
    assert ctx["job_store"].calls[-1][0] == "failed"
    assert job.status == "failed"


async def test_success_marks_succeeded(monkeypatch):
    monkeypatch.setattr(settings, "worker_max_tries", 1)
    store = _Store()
    ctx, job = _ctx(store, job_try=1)
    result = await tasks._run(ctx, str(job.id), _ok_work({"chunks": 7}))
    assert result == {"chunks": 7}
    assert ctx["job_store"].calls == [("running", None), ("succeeded", {"chunks": 7})]


async def test_cancellation_terminal_marks_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worker_max_tries", 1)
    monkeypatch.setattr(settings, "audit_log_path", tmp_path / "audit.jsonl")
    store = _Store()
    ctx, job = _ctx(store, job_try=1)

    async def hang():
        await asyncio.sleep(60)

    task = asyncio.create_task(tasks._run(ctx, str(job.id), hang()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert job.status == "failed"
    assert ctx["job_store"].calls[-1][0] == "failed"
    payload = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert payload["event"] == "job_dead_letter"
