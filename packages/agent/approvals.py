"""Human-in-the-loop approvals: per-request store + process-global bridge.

When a high-risk tool is gated to ASK (see :class:`~agent.sandbox.Sandbox`), the runtime
calls the :class:`ApprovalBridge` wired as ``ToolRuntime(approval=bridge)``. The bridge
reads the per-request :class:`ApprovalStore` bound to the current task via a contextvar
(FastAPI runs each request in its own task), so concurrent requests never share approval
state. The store:

1. emits an ``approval-request`` event (SSE + the turn's progress stream),
2. registers the decision future with the :class:`ApprovalBroker`,
3. awaits the future with :attr:`~core.config.Settings.approval_timeout_seconds` (deny on
   timeout).

``POST /approvals/{id}`` resolves the future via the broker. The broker is **distributed**
over Redis Pub/Sub (state in Redis, cross-node wakeup) so a POST hitting node B wakes the
SSE awaiting on node A; :class:`MemoryApprovalBroker` is the single-process test/dev
fallback. A tool that ASKs with no approver bound degrades to DENY — safe by default.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

import structlog
from core.config import settings

from agent.decisions import PreToolDecision, ToolExecution

_log = structlog.get_logger("deepdive.agent")

# Per-request approval store bound for the duration of one request task. The bridge denies
# when nothing is bound (tests / off-turn tool calls) — never blocks on a phantom request.
_APPROVAL_CTX: ContextVar[ApprovalStore | None] = ContextVar("approval_store", default=None)


def set_request_approval(store: ApprovalStore | None) -> None:
    """Bind ``store`` to the current task (``None`` unbinds). Call once per request."""
    _APPROVAL_CTX.set(store)


def current_approval_store() -> ApprovalStore | None:
    """The store bound to the current task, or ``None`` outside a request."""
    return _APPROVAL_CTX.get()


# ── broker: resolution routing (memory fallback + Redis pub/sub) ──
class ApprovalBroker:
    """Registry of pending approvals + cross-node resolution routing.

    A store registers its decision ``Future`` keyed by ``approval_id``; ``resolve`` completes
    it (locally for the memory broker; locally + via Redis pub/sub for the distributed one).
    ``owner`` lets the API verify the requester owns the approval before resolving.
    """

    async def register(
        self, approval_id: str, future: asyncio.Future, *, user_id: str | None = None
    ) -> None: ...
    async def unregister(self, approval_id: str) -> None: ...
    async def resolve(self, approval_id: str, allow: bool) -> bool: ...
    async def owner(self, approval_id: str) -> str | None: ...
    async def aclose(self) -> None: ...


class MemoryApprovalBroker(ApprovalBroker):
    """Single-process fallback: resolves the local future directly (tests/dev)."""

    def __init__(self) -> None:
        self._local: dict[str, tuple[asyncio.Future, str | None]] = {}

    async def register(self, approval_id, future, *, user_id=None) -> None:
        self._local[approval_id] = (future, user_id)

    async def unregister(self, approval_id) -> None:
        self._local.pop(approval_id, None)

    async def resolve(self, approval_id, allow) -> bool:
        entry = self._local.get(approval_id)
        if entry is None or entry[0].done():
            return False
        entry[0].set_result(allow)
        return True

    async def owner(self, approval_id) -> str | None:
        entry = self._local.get(approval_id)
        return entry[1] if entry is not None else None

    async def aclose(self) -> None:
        pass


class RedisApprovalBroker(ApprovalBroker):
    """Distributed broker: approval state in Redis + pub/sub wakeup across nodes.

    Every node subscribes to a shared channel. A resolution (POST /approvals on any node)
    publishes ``{approval_id, allow}``; each node's listener resolves its local future. The
    Redis key ``approval:{id}`` holds the owner + final status so a late read still sees the
    outcome even if the originating node restarted. ``redis`` is a duck-typed ``redis.asyncio``
    client exposing ``set`` / ``publish`` / ``pubsub``.
    """

    def __init__(
        self,
        redis,
        *,
        channel: str = "approval:resolutions",
        state_prefix: str = "approval:",
        ttl_s: int = 3600,
    ) -> None:
        self._redis = redis
        self._channel = channel
        self._state_prefix = state_prefix
        self._ttl = ttl_s
        self._local: dict[str, tuple[asyncio.Future, str | None]] = {}
        self._listener: asyncio.Task | None = None

    async def register(self, approval_id, future, *, user_id=None) -> None:
        self._local[approval_id] = (future, user_id)
        await self._redis.set(
            f"{self._state_prefix}{approval_id}",
            json.dumps({"user_id": user_id, "status": "pending"}),
            ex=self._ttl,
        )
        if self._listener is None:
            self._listener = asyncio.create_task(self._listen())

    async def unregister(self, approval_id) -> None:
        self._local.pop(approval_id, None)

    async def resolve(self, approval_id, allow) -> bool:
        # Local fast path (also idempotent with the listener below via done() checks).
        entry = self._local.get(approval_id)
        if entry is not None and not entry[0].done():
            entry[0].set_result(allow)
        status = "allowed" if allow else "denied"
        await self._redis.set(
            f"{self._state_prefix}{approval_id}",
            json.dumps({"user_id": entry[1] if entry else None, "status": status}),
            ex=self._ttl,
        )
        await self._redis.publish(
            self._channel, json.dumps({"approval_id": approval_id, "allow": allow})
        )
        return True

    async def owner(self, approval_id) -> str | None:
        raw = await self._redis.get(f"{self._state_prefix}{approval_id}")
        if raw is None:
            return None
        try:
            return json.loads(raw).get("user_id")
        except (TypeError, ValueError):
            return None

    async def _listen(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                except (TypeError, ValueError):
                    continue
                entry = self._local.get(data.get("approval_id"))
                if entry is not None and not entry[0].done():
                    entry[0].set_result(data.get("allow", False))
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.close()

    async def aclose(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass  # deliberate cancellation — expected unwind
            except Exception:  # noqa: BLE001 - best-effort shutdown; never mask teardown
                _log.warning("approval_listener_shutdown_error", exc_info=True)
            self._listener = None


# ── store + bridge ──
class ApprovalStore:
    """Per-request approval state: emits request events, awaits decisions with a timeout."""

    def __init__(
        self,
        broker: ApprovalBroker | None = None,
        *,
        user_id: str | None = None,
        sink: Callable[[dict], None] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._broker = broker or MemoryApprovalBroker()
        self._user_id = user_id
        self._sink = sink or (lambda _event: None)
        self._timeout_s = timeout_s if timeout_s is not None else settings.approval_timeout_seconds

    async def request(self, exec_: ToolExecution, decision: PreToolDecision) -> PreToolDecision:
        """Emit an approval request and block until resolved (or timeout → deny)."""
        approval_id = str(uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        event = {
            "type": "approval-request",
            "data": {
                "approval_id": approval_id,
                "name": exec_.name,
                "arguments": exec_.arguments,
                "reason": decision.reason,
            },
        }
        # Delivery is exclusively via ``self._sink`` (the request's SSE pump); a turn-level
        # progress sink is NOT used here so an approval frame is never emitted twice.
        self._sink(event)
        try:
            await self._broker.register(approval_id, future, user_id=self._user_id)
            try:
                allowed = await asyncio.wait_for(future, self._timeout_s)
            except TimeoutError:
                allowed = False
        finally:
            await self._broker.unregister(approval_id)
        if allowed:
            return PreToolDecision.allow()
        return PreToolDecision.deny(decision.reason or "approval not granted (denied or timed out)")

    async def resolve(self, approval_id: str, allow: bool) -> bool:
        """Resolve a pending approval (used by tests / direct store callers)."""
        return await self._broker.resolve(approval_id, allow)


class ApprovalBridge:
    """Process-global approval entry point wired as ``ToolRuntime(approval=bridge)``.

    Reads the per-request store from the contextvar; denies when none is bound (safe default).
    ``resolve`` routes to the broker so ``POST /approvals/{id}`` works cross-node.
    """

    def __init__(self, broker: ApprovalBroker | None = None) -> None:
        self.broker = broker or MemoryApprovalBroker()

    async def __call__(
        self, exec_: ToolExecution, decision: PreToolDecision
    ) -> PreToolDecision:
        store = _APPROVAL_CTX.get()
        if store is None:
            return PreToolDecision.deny(
                decision.reason or "approval required but no approver bound to this request"
            )
        return await store.request(exec_, decision)

    async def resolve(self, approval_id: str, allow: bool) -> bool:
        return await self.broker.resolve(approval_id, allow)

    async def owner(self, approval_id: str) -> str | None:
        return await self.broker.owner(approval_id)

    async def aclose(self) -> None:
        await self.broker.aclose()


# ── process-global bridge (configured once at API startup) ──
_bridge: ApprovalBridge | None = None


def get_approval_bridge() -> ApprovalBridge:
    """The process-global :class:`ApprovalBridge` (memory broker until configured)."""
    global _bridge
    if _bridge is None:
        _bridge = ApprovalBridge()
    return _bridge


def configure_approval_broker(redis: Any) -> None:
    """Swap in the distributed Redis broker (call once at API startup when Redis is up).

    Until configured, the bridge uses :class:`MemoryApprovalBroker` (dev/tests).
    """
    bridge = get_approval_bridge()
    if not isinstance(bridge.broker, RedisApprovalBroker):
        bridge.broker = RedisApprovalBroker(redis)
