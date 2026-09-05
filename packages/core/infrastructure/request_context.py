"""Per-request user identity + LLM channel for tenant-isolated retrieval.

The RAG pipeline and memory recallers are process-wide singletons (``@lru_cache``), so
per-request identity can't be threaded through their constructors. The ``/chat`` endpoint
sets :data:`request_user` before the agent runs; recallers read it as a fallback when they
were built without a concrete user. ``None`` means an anonymous guest (no drive assets /
memory of their own).

:data:`request_llm_channel` is the request's *effective LLM channel* ``(model, base_url,
api_key)`` — the same tuple the agent conversation is pinned to. The agent loop threads it
into every conversation call, but in-pipeline LLM sub-calls (rag_search query rewrite / CRAG
judge) call the shared client with no per-call override, so without this they would ride the
process-global default channel (in the worker: the unconfigured llm-gateway → upstream 401).
``/chat`` and the worker's ``research_drive``/``run_agent_turn`` set it before the agent runs;
the retrieval shim in :mod:`api.agent_factory` forwards it per call. ``None`` means the
configured global client is used unchanged.
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar

request_user: ContextVar[uuid.UUID | None] = ContextVar("request_user", default=None)
request_llm_channel: ContextVar[tuple[str | None, str | None, str | None] | None] = ContextVar(
    "request_llm_channel", default=None
)


def get_request_user_id() -> uuid.UUID | None:
    """Return the current request's user id, or ``None`` for a guest / no request."""
    return request_user.get()


def set_request_user(user_id: uuid.UUID | None) -> None:
    request_user.set(user_id)


def get_request_llm_channel() -> tuple[str | None, str | None, str | None] | None:
    """Return the current request's ``(model, base_url, api_key)`` channel, or ``None``."""
    return request_llm_channel.get()


def set_request_llm_channel(
    channel: tuple[str | None, str | None, str | None] | None,
) -> None:
    """Pin the current request's LLM channel so context-free sub-calls (rag rewrite / CRAG)
    use the same key/model as the conversation instead of the process-global default."""
    request_llm_channel.set(channel)
