"""gRPC service implementation: map proto requests onto the in-process RAG pipeline.

Every ``Retrieve`` is gated by :class:`AuthGuard` before reaching the pipeline:

- **token** — a shared secret from gRPC metadata (``authorization: Bearer <token>``). An
  empty ``token`` disables auth so local development needs no secret.
- **rate limit** — a per-peer token bucket when ``rate_limit > 0`` (0 = unlimited).
- **tenant binding** — the pipeline only applies its owner / workspace / ACL visibility
  predicate when a ``user_id`` filter is present; a request without one reads across every
  tenant. The guard therefore **requires** a non-empty ``user_id`` (or an explicit
  ``guest=1`` marker) and rejects anything else with ``PERMISSION_DENIED``. A guest
  resolves to ``user_id=None``, which the recall nodes treat as public-link assets only.
"""
import json
import time

import grpc
from core.infrastructure.proto import retrieval_pb2, retrieval_pb2_grpc


class TokenBucketLimiter:
    """Minimal per-key token bucket: capacity 1, refilled at ``rate`` tokens/second."""

    def __init__(self, rate: float = 0) -> None:
        self.rate = rate
        self._tokens: dict[str, float] = {}
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        if self.rate <= 0:
            return True
        now = time.monotonic()
        bucket = min(1.0, self._tokens.get(key, 1.0) + (now - self._last.get(key, now)) * self.rate)
        self._tokens[key], self._last[key] = bucket, now
        if bucket >= 1.0:
            self._tokens[key] = bucket - 1.0
            return True
        return False


class AuthGuard:
    """Per-call gate: shared-secret token, per-peer rate limit, tenant binding.

    The guard only aborts; a passing call returns the normalized ``filters`` for the
    pipeline. ``context.abort`` raises a ``grpc.RpcError``, so a denied call never reaches
    the handler body.
    """

    def __init__(self, token: str = "", rate_limit: int = 0) -> None:
        self.token = token
        self._limiter = TokenBucketLimiter(rate_limit)

    @staticmethod
    def _bearer(metadata) -> str:
        for key, value in metadata or ():
            if key.lower() == "authorization":
                return value.removeprefix("Bearer ").strip()
        return ""

    def require_token(self, context) -> None:
        """Abort UNAUTHENTICATED when a configured token is missing / wrong."""
        if not self.token:
            return  # auth disabled (dev)
        if self._bearer(context.invocation_metadata()) != self.token:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or missing retrieval token")

    def rate_limit(self, context) -> None:
        """Abort RESOURCE_EXHAUSTED when the caller's per-peer bucket is empty."""
        if not self._limiter.allow(context.peer() or "unknown"):
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "retrieval rate limit exceeded")

    def bind_tenant(self, request, context) -> dict:
        """Enforce tenant isolation and return the pipeline filters for this request.

        ``user_id`` is required; a guest request (no user) must say so explicitly with
        ``guest=1`` and is normalized to ``user_id=None`` (public-link assets only). Any
        request without a tenant scope — including a bare ``domain_id`` — is refused.
        """
        filters = dict(request.filters) if request.filters else {}
        uid = (filters.get("user_id") or "").strip()
        is_guest = filters.get("guest") in ("1", "true", "yes")
        if not uid and not is_guest:
            context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "user_id filter is required for tenant isolation",
            )
        if not uid:
            filters.pop("guest", None)  # marker consumed; normalize to a public-only scope
            filters["user_id"] = None
        return filters


class RetrievalService(retrieval_pb2_grpc.RetrievalServiceServicer):
    def __init__(self, pipeline, auth: AuthGuard | None = None) -> None:
        self.pipeline = pipeline
        self.auth = auth or AuthGuard()

    async def Retrieve(self, request, context):
        self.auth.require_token(context)
        self.auth.rate_limit(context)
        filters = self.auth.bind_tenant(request, context)
        top_k = request.top_k if request.top_k > 0 else 5
        hits = await self.pipeline.retrieve(request.query, top_k, filters)
        return retrieval_pb2.RetrieveResponse(
            hits=[
                retrieval_pb2.SearchHit(
                    id=h["id"],
                    text=h["text"],
                    score=h["score"],
                    meta=json.dumps(h.get("meta"), ensure_ascii=False) if h.get("meta") else "",
                )
                for h in hits
            ]
        )

    async def Health(self, request, context):
        # Liveness probe: deliberately unauthenticated (leaks no data).
        return retrieval_pb2.HealthResponse(status="ok")
