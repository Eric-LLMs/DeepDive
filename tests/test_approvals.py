"""Human-in-the-loop approvals: store, bridge, memory + Redis brokers."""
import asyncio
import json

from agent.security.approvals import (
    ApprovalBridge,
    ApprovalStore,
    MemoryApprovalBroker,
    RedisApprovalBroker,
    current_approval_store,
    set_request_approval,
)
from agent.engine.decisions import PreToolDecision, ToolExecution


def _ask(exec_: ToolExecution) -> PreToolDecision:
    return PreToolDecision.ask(f"approve {exec_.name}")


async def test_store_emits_approval_request_to_sink():
    emitted = []
    broker = MemoryApprovalBroker()
    store = ApprovalStore(broker, user_id="u1", sink=emitted.append)
    exec_ = ToolExecution(call_id="c1", name="write_file", arguments={"path": "/tmp/x"})

    task = asyncio.create_task(store.request(exec_, _ask(exec_)))
    while not emitted:
        await asyncio.sleep(0.001)

    event = emitted[0]
    assert event["type"] == "approval-request"
    assert event["data"]["name"] == "write_file"
    assert event["data"]["reason"] == "approve write_file"
    assert event["data"]["arguments"] == {"path": "/tmp/x"}
    approval_id = event["data"]["approval_id"]

    await broker.resolve(approval_id, True)
    decision = await task
    assert decision.kind == "allow"


async def test_store_deny_path():
    emitted = []
    broker = MemoryApprovalBroker()
    store = ApprovalStore(broker, user_id="u1", sink=emitted.append)
    exec_ = ToolExecution(call_id="c1", name="write_file", arguments={})

    task = asyncio.create_task(store.request(exec_, _ask(exec_)))
    while not emitted:
        await asyncio.sleep(0.001)
    approval_id = emitted[0]["data"]["approval_id"]

    await broker.resolve(approval_id, False)
    decision = await task
    assert decision.kind == "deny"
    assert decision.reason == "approve write_file"


async def test_store_denies_on_timeout():
    store = ApprovalStore(MemoryApprovalBroker(), user_id="u1", timeout_s=0.01)
    exec_ = ToolExecution(call_id="c1", name="write_file", arguments={})

    decision = await store.request(exec_, _ask(exec_))
    assert decision.kind == "deny"
    assert decision.reason == "approve write_file"


async def test_bridge_denies_when_no_store_bound():
    bridge = ApprovalBridge(MemoryApprovalBroker())
    exec_ = ToolExecution(call_id="c1", name="write_file", arguments={})

    decision = await bridge(exec_, PreToolDecision.ask())
    assert decision.kind == "deny"
    assert "no approver bound" in decision.reason


async def test_bridge_routes_to_bound_store():
    emitted = []
    broker = MemoryApprovalBroker()
    store = ApprovalStore(broker, user_id="u1", sink=emitted.append)
    set_request_approval(store)
    try:
        exec_ = ToolExecution(call_id="c1", name="write_file", arguments={})
        bridge = ApprovalBridge(broker)

        task = asyncio.create_task(bridge(exec_, _ask(exec_)))
        while not emitted:
            await asyncio.sleep(0.001)
        approval_id = emitted[0]["data"]["approval_id"]

        await broker.resolve(approval_id, True)
        decision = await task
        assert decision.kind == "allow"
    finally:
        set_request_approval(None)

    assert current_approval_store() is None


async def test_broker_tracks_owner():
    broker = MemoryApprovalBroker()
    future = asyncio.get_running_loop().create_future()
    await broker.register("a1", future, user_id="u1")

    assert await broker.owner("a1") == "u1"
    assert await broker.owner("missing") is None

    await broker.unregister("a1")
    assert await broker.owner("a1") is None


# ── Redis pub/sub broker ──
class _FakePubSub:
    def __init__(self, redis):
        self._redis = redis
        self._queue = asyncio.Queue()
        self._channel = None
        self._subscribed = False

    async def subscribe(self, channel):
        self._channel = channel
        self._subscribed = True
        self._redis._subscribers.append(self)

    async def _deliver(self, channel, message):
        if self._subscribed and channel == self._channel:
            await self._queue.put({"type": "message", "data": message})

    async def listen(self):
        while True:
            yield await self._queue.get()

    async def close(self):
        if self in self._redis._subscribers:
            self._redis._subscribers.remove(self)


class _FakeRedis:
    def __init__(self):
        self._store = {}
        self._subscribers = []

    async def set(self, key, value, ex=None):
        self._store[key] = value

    async def get(self, key):
        return self._store.get(key)

    async def publish(self, channel, message):
        for sub in list(self._subscribers):
            await sub._deliver(channel, message)
        return 0

    def pubsub(self):
        return _FakePubSub(self)


async def _wait_for_subscription(redis: _FakeRedis) -> None:
    """Let broker_a's background ``_listen`` task finish subscribing (deterministic)."""
    for _ in range(200):
        if redis._subscribers:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("pub/sub listener never subscribed")


async def test_redis_broker_cross_node_wakeup():
    redis = _FakeRedis()
    # Node A: a turn is blocked awaiting an approval (its broker registered the future and
    # listens for pub/sub wakeups).
    broker_a = RedisApprovalBroker(redis)
    future = asyncio.get_running_loop().create_future()
    await broker_a.register("a1", future, user_id="u1")
    await _wait_for_subscription(redis)
    assert await broker_a.owner("a1") == "u1"

    # Node B (a different broker instance over the same Redis): the user POSTs /approvals/a1.
    broker_b = RedisApprovalBroker(redis)
    await broker_b.resolve("a1", True)

    assert await asyncio.wait_for(future, 1) is True
    assert json.loads(await redis.get("approval:a1"))["status"] == "allowed"

    await broker_a.aclose()
    await broker_b.aclose()


async def test_redis_broker_deny_via_pubsub():
    redis = _FakeRedis()
    broker_a = RedisApprovalBroker(redis)
    future = asyncio.get_running_loop().create_future()
    await broker_a.register("a2", future, user_id="u1")
    await _wait_for_subscription(redis)

    broker_b = RedisApprovalBroker(redis)
    await broker_b.resolve("a2", False)

    assert await asyncio.wait_for(future, 1) is False

    await broker_a.aclose()
    await broker_b.aclose()
