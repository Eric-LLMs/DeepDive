"""Redis query cache for RAG retrieval: hit/miss, config & corpus invalidation, degradation."""
from __future__ import annotations

import pytest
from core.config import settings
from rag.query_cache import (
    CachedRetriever,
    _redis,
    bump_corpus_version,
    configure_query_cache,
    wrap_retriever,
)


class FakeRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.hits = [{"id": "c1", "score": 0.9, "text": "chunk one"}]

    async def retrieve(self, query, top_k=5, filters=None):
        self.calls.append((query, top_k, filters))
        return self.hits


class FakeRedis:
    """Minimal in-memory Redis fake: get/setex/incr."""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.counter = 0

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def incr(self, key):
        self.counter += 1
        self.store[key] = str(self.counter)
        return self.counter


@pytest.fixture(autouse=True)
def _unbind_redis():
    configure_query_cache(None)
    yield
    configure_query_cache(None)


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


def _inner():
    return FakeRetriever()


async def test_hit_serves_cached_value_without_inner_call(redis):
    configure_query_cache(redis)
    inner = _inner()
    cached = CachedRetriever(inner, ttl_seconds=60)

    first = await cached.retrieve("hello", top_k=5, filters={"user_id": "u1"})
    assert first == inner.hits
    assert len(inner.calls) == 1

    second = await cached.retrieve("hello", top_k=5, filters={"user_id": "u1"})
    assert second == inner.hits
    assert len(inner.calls) == 1  # served from cache, inner untouched


async def test_different_filters_are_different_keys(redis):
    configure_query_cache(redis)
    inner = _inner()
    cached = CachedRetriever(inner, ttl_seconds=60)

    await cached.retrieve("hello", top_k=5, filters={"user_id": "u1"})
    await cached.retrieve("hello", top_k=5, filters={"user_id": "u2"})
    assert len(inner.calls) == 2


async def test_config_change_invalidates_cache(redis, monkeypatch):
    configure_query_cache(redis)
    inner = _inner()
    cached = CachedRetriever(inner, ttl_seconds=60)

    monkeypatch.setattr("rag.query_cache.config_version", lambda: "cfg-v1")
    await cached.retrieve("q", top_k=5, filters=None)

    # New config → different key → cache miss even though query/filters match.
    monkeypatch.setattr("rag.query_cache.config_version", lambda: "cfg-v2")
    await cached.retrieve("q", top_k=5, filters=None)
    assert len(inner.calls) == 2


async def test_corpus_bump_invalidates_cache(redis):
    configure_query_cache(redis)
    inner = _inner()
    cached = CachedRetriever(inner, ttl_seconds=60)

    await cached.retrieve("q", top_k=5, filters=None)
    assert len(inner.calls) == 1

    # Reindex/ingest bumps the corpus version → the version-suffixed key changes.
    await bump_corpus_version(redis)
    await cached.retrieve("q", top_k=5, filters=None)
    assert len(inner.calls) == 2


async def test_redis_down_passes_through(redis):
    configure_query_cache(redis)

    class DownRedis(FakeRedis):
        async def get(self, key):
            raise ConnectionError("redis unavailable")

    cached = CachedRetriever(_inner(), ttl_seconds=60)
    configure_query_cache(DownRedis())

    hits = await cached.retrieve("q", top_k=5, filters=None)
    assert hits  # a retrieval is never failed by the cache


async def test_no_redis_bound_passes_through():
    inner = _inner()
    cached = CachedRetriever(inner, ttl_seconds=60)
    assert _redis() is None

    hits = await cached.retrieve("q", top_k=5, filters=None)
    assert hits == inner.hits
    assert len(inner.calls) == 1


async def test_cache_write_failure_is_non_fatal(redis):
    configure_query_cache(redis)
    inner = _inner()

    class WriteDownRedis(FakeRedis):
        async def setex(self, key, ttl, value):
            raise ConnectionError("redis unavailable")

    cached = CachedRetriever(inner, ttl_seconds=60)
    configure_query_cache(WriteDownRedis())

    hits = await cached.retrieve("q", top_k=5, filters=None)
    assert hits == inner.hits  # retrieval succeeded; cache write swallowed


def test_disabled_ttl_returns_inner_untouched(monkeypatch):
    monkeypatch.setattr(settings, "query_cache_ttl_seconds", 0)
    inner = _inner()
    assert wrap_retriever(inner) is inner


def test_enabled_ttl_wraps(monkeypatch):
    monkeypatch.setattr(settings, "query_cache_ttl_seconds", 300)
    inner = _inner()
    wrapped = wrap_retriever(inner)
    assert isinstance(wrapped, CachedRetriever)
    assert wrapped._inner is inner


async def test_bump_corpus_version_is_never_raising(redis):
    class BoomRedis(FakeRedis):
        async def incr(self, key):
            raise RuntimeError("redis down")

    configure_query_cache(redis)
    await bump_corpus_version(None)  # no client → no-op
    await bump_corpus_version(BoomRedis())  # failure → swallowed, no raise
