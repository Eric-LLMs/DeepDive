"""Async job queue: gateway enqueues, worker executes, PostgreSQL is the source of truth.

Two collaborators share one contract:

- :class:`JobStore` persists a job row (the ``jobs`` table) and drives its status
  transitions. The gateway and the worker both read/write it.
- :class:`TaskQueue` pairs the store with an arq Redis pool: ``enqueue`` first creates the
  PG job row (so ``GET /jobs/{id}`` is answerable immediately), then hands the job to arq.
  arq's internal redis job id is only for delivery; PG is authoritative for status/result.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from core.infrastructure.db import JobModel

# Task names shared by the gateway (enqueue) and the worker (WorkerSettings.functions).
TTS = "tts"
IMAGE_FETCH = "image_fetch"
EXPLAIN = "explain"
GENERATE_DEFINITION = "generate_definition"
ANALYZE_SYNTAX = "analyze_syntax"
INDEX_SENTENCES = "index_sentences"
SESSION_FINALIZE = "session_finalize"

# Job status values.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


class JobStore:
    """Persist job rows and drive status transitions."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    async def create(self, type_: str, payload: dict) -> JobModel:
        async with self.session_factory() as session:
            job = JobModel(type=type_, status=QUEUED, payload=payload)
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return job

    async def get(self, job_id: uuid.UUID) -> JobModel | None:
        async with self.session_factory() as session:
            return await session.get(JobModel, job_id)

    async def mark_running(self, job_id: uuid.UUID) -> None:
        async with self.session_factory() as session:
            job = await session.get(JobModel, job_id)
            if job is not None:
                job.status = RUNNING
                job.started_at = datetime.now(timezone.utc)
                await session.commit()

    async def mark_succeeded(self, job_id: uuid.UUID, result: dict) -> None:
        async with self.session_factory() as session:
            job = await session.get(JobModel, job_id)
            if job is not None:
                job.status = SUCCEEDED
                job.result = result
                job.completed_at = datetime.now(timezone.utc)
                await session.commit()

    async def mark_failed(self, job_id: uuid.UUID, error: str) -> None:
        async with self.session_factory() as session:
            job = await session.get(JobModel, job_id)
            if job is not None:
                job.status = FAILED
                job.error = error
                job.completed_at = datetime.now(timezone.utc)
                await session.commit()


class TaskQueue:
    """Enqueue jobs onto arq and read their state back from PostgreSQL."""

    def __init__(self, redis, job_store: JobStore) -> None:
        self.redis = redis
        self.job_store = job_store

    async def enqueue(self, type_: str, payload: dict) -> uuid.UUID:
        job = await self.job_store.create(type_, payload)
        # arq resolves `type_` against WorkerSettings.functions by name; job_id is passed
        # as a string so the worker can reconstruct the UUID regardless of serializer.
        await self.redis.enqueue_job(type_, str(job.id), payload)
        return job.id

    async def get(self, job_id: uuid.UUID) -> dict[str, Any]:
        job = await self.job_store.get(job_id)
        if job is None:
            return {"status": "unknown", "result": None, "error": "job not found"}
        return {"status": job.status, "result": job.result, "error": job.error}
