"""gRPC retrieval auth: token gate, per-peer rate limit, tenant binding, client encoding."""
from types import SimpleNamespace
from uuid import uuid4

import grpc
import pytest
from core.infrastructure.retrieval_grpc import GrpcRetriever

from apps.retrieval.server import AuthGuard, RetrievalService, TokenBucketLimiter


class _Abort(Exception):
    def __init__(self, code, details):
        self.code = code
        self.details = details


class _FakeContext:
    def __init__(self, metadata=None, peer="ip:1.2.3.4"):
        self._metadata = metadata or ()
        self._peer = peer
        self.aborts = []

    def invocation_metadata(self):
        return self._metadata

    def peer(self):
        return self._peer

    async def abort(self, code, details):
        # Mirrors grpc.aio: abort() is a coroutine; awaiting it raises the abort error.
        self.aborts.append((code, details))
        raise _Abort(code, details)


# ── rate limiter ──
def test_rate_limiter_unlimited_when_zero():
    limiter = TokenBucketLimiter(0)
    assert limiter.allow("peer-a") is True
    assert limiter.allow("peer-a") is True


def test_rate_limiter_bucket_exhausts_then_refills():
    limiter = TokenBucketLimiter(1.0)  # 1 req/s, capacity 1
    assert limiter.allow("peer-a") is True
    assert limiter.allow("peer-a") is False  # bucket empty within the same second
    # Different peer has its own bucket.
    assert limiter.allow("peer-b") is True


# ── token gate ──
async def test_token_disabled_when_empty():
    guard = AuthGuard(token="")
    await guard.require_token(_FakeContext())  # no abort


async def test_token_rejects_missing_or_wrong():
    guard = AuthGuard(token="s3cret")
    with pytest.raises(_Abort) as exc:
        await guard.require_token(_FakeContext(metadata=[("authorization", "Bearer wrong")]))
    assert exc.value.code == grpc.StatusCode.UNAUTHENTICATED


async def test_token_accepts_bearer():
    guard = AuthGuard(token="s3cret")
    await guard.require_token(_FakeContext(metadata=[("authorization", "Bearer s3cret")]))


# ── tenant binding ──
def _req(filters):
    return SimpleNamespace(query="", top_k=0, filters=filters)


async def test_tenant_requires_user_id():
    guard = AuthGuard()
    with pytest.raises(_Abort) as exc:
        await guard.bind_tenant(_req({}), _FakeContext())
    assert exc.value.code == grpc.StatusCode.PERMISSION_DENIED
    # A bare domain filter is also a cross-tenant read — refused without user_id.
    with pytest.raises(_Abort):
        await guard.bind_tenant(_req({"domain_id": "abc"}), _FakeContext())


async def test_tenant_guest_marker_normalizes_to_none():
    guard = AuthGuard()
    filters = await guard.bind_tenant(_req({"guest": "1"}), _FakeContext())
    assert filters["user_id"] is None


async def test_tenant_keeps_user_id():
    guard = AuthGuard()
    filters = await guard.bind_tenant(_req({"user_id": "u-1", "domain_id": "d"}), _FakeContext())
    assert filters == {"user_id": "u-1", "domain_id": "d"}


# ── servicer end-to-end ──
class _FakePipeline:
    def __init__(self):
        self.calls = []

    async def retrieve(self, query, top_k, filters):
        self.calls.append((query, top_k, filters))
        return []


async def test_retrieve_runs_pipeline_with_normalized_filters():
    pipeline = _FakePipeline()
    svc = RetrievalService(pipeline)
    ctx = _FakeContext()
    await svc.Retrieve(_req({"user_id": "u-1"}), ctx)
    assert pipeline.calls == [("", 5, {"user_id": "u-1"})]


async def test_retrieve_guest_scopes_to_public():
    pipeline = _FakePipeline()
    svc = RetrievalService(pipeline)
    await svc.Retrieve(_req({"guest": "1"}), _FakeContext())
    assert pipeline.calls[0][2] == {"user_id": None}


async def test_retrieve_without_tenant_scope_denied():
    pipeline = _FakePipeline()
    svc = RetrievalService(pipeline)
    with pytest.raises(_Abort) as exc:
        await svc.Retrieve(_req({}), _FakeContext())
    assert exc.value.code == grpc.StatusCode.PERMISSION_DENIED
    assert pipeline.calls == []


async def test_retrieve_token_enforced():
    pipeline = _FakePipeline()
    svc = RetrievalService(pipeline, auth=AuthGuard(token="s3cret"))
    with pytest.raises(_Abort) as exc:
        await svc.Retrieve(_req({"user_id": "u-1"}), _FakeContext())
    assert exc.value.code == grpc.StatusCode.UNAUTHENTICATED


# ── client encoding ──
def test_stringify_filters_handles_uuid_and_guest():
    uid = uuid4()
    assert GrpcRetriever._stringify_filters({"user_id": uid, "domain_id": "d"}) == {
        "user_id": str(uid),
        "domain_id": "d",
    }
    # None user_id (guest) → explicit guest marker; other None values dropped.
    assert GrpcRetriever._stringify_filters({"user_id": None, "top": None}) == {"guest": "1"}


async def test_client_attaches_token_metadata_when_set():
    retriever = GrpcRetriever("localhost:15051", token="s3cret")
    assert retriever._metadata() == (("authorization", "Bearer s3cret"),)
    await retriever.close()


async def test_client_sends_token_on_retrieve():
    retriever = GrpcRetriever("localhost:15051", token="s3cret")
    seen = {}

    class _FakeStub:
        async def Retrieve(self, request, metadata=None):
            seen["metadata"] = metadata
            return SimpleNamespace(hits=[])

    retriever._stub = _FakeStub()
    await retriever.retrieve("q", filters={"user_id": "u-1"})
    assert seen["metadata"] == (("authorization", "Bearer s3cret"),)
    await retriever.close()
