"""Cross-process wake-up bus (Redis Pub/Sub, invalidation hints only).

DeepDive already ships a Redis pool on both sides of the process boundary (the API's
``app.state.redis`` and the worker's ``ctx["redis"]``). This module exposes a module-level
*set at startup / cleared at shutdown* publish handle so fire-and-forget wake-up events
(the research monitor's "something changed, refetch the task") can be emitted from either
process without threading a client through every call site.

Semantics are deliberately weak: a published message is a **hint**, never a reliable
delivery contract. If nobody is subscribed (or Redis is briefly down) the hint is dropped
and the client's own poll / reconnect-snapshot path still converges. No caller ever blocks
or fails because a hint could not be delivered.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_client: Any = None  # redis.asyncio.Redis, installed by the host app at startup


def set_bus(client: Any) -> None:
    """Install the process's Redis client (API lifespan / worker startup)."""
    global _client
    _client = client


def unset_bus() -> None:
    """Clear the bus handle (process shutdown)."""
    global _client
    _client = None


async def publish(channel: str, message: dict) -> None:
    """Best-effort publish of ``message`` (JSON) on ``channel``. Never raises."""
    client = _client
    if client is None:
        return
    try:
        await client.publish(channel, json.dumps(message, ensure_ascii=False, default=str))
    except Exception:
        logger.debug("redis_bus publish failed on %s (dropped hint)", channel, exc_info=True)
