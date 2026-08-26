"""Redis query cache for RAG retrieval.

Wraps any :class:`Retriever` (in-process :class:`RAGPipeline` or the gRPC client) with a
Redis cache keyed by ``(query, filters, top_k, node-config-version, corpus-version)``:

- **config change** — the key embeds a hash of the current node config, so a new topology
  invalidates automatically (no explicit flush needed).
- **corpus change** — a ``rag:corpus_version`` counter is bumped on every ingest / reindex;
  the version is part of the key, so stale hits vanish as soon as the corpus moves.

Degradation contract: the cache is a pure accelerator. Redis being down, a missing value,
or a write failure all fall through to the wrapped retriever — a retrieval is never failed
by the cache. When the cache is disabled (``query_cache_ttl_seconds <= 0``) or no Redis
client has been bound, :func:`wrap_retriever` returns the inner retriever untouched.
"""
from __future__ import annotations

import hashlib
import json
import logging

from core.config import settings

from rag.config_store import current_config

logger = logging.getLogger(__name__)

_CORPUS_VERSION_KEY = "rag:corpus_version"

# The shared Redis pool, bound at app startup via configure_query_cache(redis). A plain
# module global (not contextvar) because it is a process-wide connection pool, not a
# per-request value. Tests inject a fake via the same setter.
_client = None


def configure_query_cache(redis) -> None:
    """Bind the shared Redis pool the cache uses (call once at app startup)."""
    global _client
    _client = redis


def _redis():
    return _client


def config_version() -> str:
    """Stable hash of the current node-config (changes → cache key changes)."""
    raw = json.dumps(current_config().to_dict(), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def bump_corpus_version(redis) -> None:
    """Best-effort corpus-version bump (invalidate the query cache after a reindex/ingest).

    Never raises: a cache-invalidation failure must not fail the ingest that just succeeded.
    """
    if redis is None:
        return
    try:
        await redis.incr(_CORPUS_VERSION_KEY)
    except Exception:
        logger.warning("rag corpus-version bump failed", exc_info=True)


class CachedRetriever:
    """Retriever decorator: Redis cache in front of any :class:`Retriever`."""

    def __init__(self, retriever, ttl_seconds: int) -> None:
        self._inner = retriever
        self._ttl = ttl_seconds

    def _key(self, query: str, top_k: int, filters: dict | None) -> str:
        payload = [query, top_k, filters or {}, config_version()]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        return f"rag:qc:{digest}"

    async def retrieve(self, query: str, top_k: int = 5, filters: dict | None = None) -> list[dict]:
        redis = _redis()
        if redis is None:
            return await self._inner.retrieve(query, top_k, filters)

        base_key = self._key(query, top_k, filters)
        try:
            version = await redis.get(_CORPUS_VERSION_KEY) or "0"
            cache_key = f"{base_key}:{version}"
            raw = await redis.get(cache_key)
            if raw:
                return json.loads(raw)
        except Exception:
            logger.warning("rag query cache read failed", exc_info=True)
            return await self._inner.retrieve(query, top_k, filters)

        hits = await self._inner.retrieve(query, top_k, filters)
        try:
            await redis.setex(
                cache_key, self._ttl, json.dumps(hits, ensure_ascii=False, default=str)
            )
        except Exception:
            logger.warning("rag query cache write failed", exc_info=True)
        return hits


def wrap_retriever(retriever):
    """Return ``retriever`` wrapped in :class:`CachedRetriever` when the cache is enabled."""
    if settings.query_cache_ttl_seconds <= 0:
        return retriever
    return CachedRetriever(retriever, settings.query_cache_ttl_seconds)
