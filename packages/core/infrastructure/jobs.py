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
from datetime import UTC, datetime
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
GENERATE_MEDIA = "generate_media"
ASSET_INGEST = "asset_ingest"
LEARNING_IMPORT = "learning_import"        # Learning-Platform sentences/articles → query repo
CHAT_SESSION_IMPORT = "chat_session_import"  # a whole chat session → query repo Q&A chunks
TOOLKIT_GENERATE = "toolkit_generate"        # workspace files → slides / mindmap / summary

# Job status values.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


class JobStore:
    """Persist job rows and drive status transitions."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    async def create(
        self, type_: str, payload: dict, user_id: uuid.UUID | None = None
    ) -> JobModel:
        async with self.session_factory() as session:
            job = JobModel(type=type_, status=QUEUED, payload=payload, user_id=user_id)
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return job

    async def get(self, job_id: uuid.UUID) -> JobModel | None:
        async with self.session_factory() as session:
            return await session.get(JobModel, job_id)

    async def mark_running(self, job_id: uuid.UUID, error: str | None = None) -> None:
        async with self.session_factory() as session:
            job = await session.get(JobModel, job_id)
            if job is not None:
                job.status = RUNNING
                job.started_at = datetime.now(UTC)
                if error is not None:
                    job.error = error
                await session.commit()

    async def mark_succeeded(self, job_id: uuid.UUID, result: dict) -> None:
        async with self.session_factory() as session:
            job = await session.get(JobModel, job_id)
            if job is not None:
                job.status = SUCCEEDED
                job.result = result
                job.completed_at = datetime.now(UTC)
                await session.commit()

    async def mark_failed(self, job_id: uuid.UUID, error: str) -> None:
        async with self.session_factory() as session:
            job = await session.get(JobModel, job_id)
            if job is not None:
                job.status = FAILED
                job.error = error
                job.completed_at = datetime.now(UTC)
                await session.commit()


class TaskQueue:
    """Enqueue jobs onto arq and read their state back from PostgreSQL."""

    def __init__(self, redis, job_store: JobStore) -> None:
        self.redis = redis
        self.job_store = job_store

    async def enqueue(
        self, type_: str, payload: dict, user_id: uuid.UUID | None = None
    ) -> uuid.UUID:
        job = await self.job_store.create(type_, payload, user_id=user_id)
        # arq resolves `type_` against WorkerSettings.functions by name; job_id is passed
        # as a string so the worker can reconstruct the UUID regardless of serializer.
        # If Redis delivery fails, don't leave the PG row stuck "queued" forever: mark it
        # failed (so the status endpoint reports an error instead of a phantom pending job)
        # and re-raise for the caller to surface as a 5xx.
        try:
            await self.redis.enqueue_job(type_, str(job.id), payload)
        except Exception as exc:
            await self.job_store.mark_failed(job.id, f"enqueue failed: {exc}")
            raise
        return job.id

    async def get(self, job_id: uuid.UUID) -> dict[str, Any]:
        job = await self.job_store.get(job_id)
        if job is None:
            return {"status": "unknown", "result": None, "error": "job not found"}
        return {
            "status": job.status,
            "result": job.result,
            "error": job.error,
            "user_id": str(job.user_id) if job.user_id is not None else None,
        }
