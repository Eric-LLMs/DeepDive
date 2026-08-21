"""Per-request user identity for tenant-isolated retrieval.

The RAG pipeline and memory recallers are process-wide singletons (``@lru_cache``), so
per-request identity can't be threaded through their constructors. The ``/chat`` endpoint
sets :data:`request_user` before the agent runs; recallers read it as a fallback when they
were built without a concrete user. ``None`` means an anonymous guest (no drive assets /
memory of their own).
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar

request_user: ContextVar[uuid.UUID | None] = ContextVar("request_user", default=None)


def get_request_user_id() -> uuid.UUID | None:
    """Return the current request's user id, or ``None`` for a guest / no request."""
    return request_user.get()


def set_request_user(user_id: uuid.UUID | None) -> None:
    request_user.set(user_id)
