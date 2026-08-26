"""ReliableLLM: a thin reliability wrapper around any :class:`~agent.loop.AgentLLMPort`.

Every agent LLM call gets:

- a **hard timeout** (``asyncio.wait_for``) — a hung provider no longer stalls the turn;
- **retry with exponential backoff** on temporary errors (timeout / 429 / 5xx / connection);
- **cancellation** — ``CancelledError`` is a ``BaseException`` and flows straight through
  ``wait_for`` and tenacity, so SSE disconnects abort the underlying request immediately.

For streams, the timeout bounds the *first token* (the window that can hang indefinitely);
after the first token arrives, deltas forward untouched (mid-stream failures are surfaced
to the loop, which handles them as a step error).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.llm_errors import LLMTemporaryError, classify


class ReliableLLM:
    def __init__(
        self,
        port,
        *,
        timeout_s: float = 90.0,
        max_retries: int = 2,
        backoff: float = 1.0,
    ) -> None:
        self._inner = port
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff = backoff

    def _retry(self, fn: Callable[[], Awaitable]):
        """Run ``fn`` with timeout+retry semantics; returns the successful result."""
        retryer = AsyncRetrying(
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_exponential(multiplier=self.backoff, max=15.0),
            retry=retry_if_exception_type(LLMTemporaryError),
            reraise=True,
        )
        return retryer(fn)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> dict:
        async def attempt() -> dict:
            try:
                return await asyncio.wait_for(
                    self._inner.chat(
                        messages, tools=tools, model=model, base_url=base_url, api_key=api_key
                    ),
                    timeout=self.timeout_s,
                )
            except BaseException as exc:
                raise classify(exc) from exc

        return await self._retry(attempt)

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[dict]:
        self._gen: AsyncIterator[dict] | None = None

        async def open_stream() -> dict:
            gen = self._inner.chat_stream(
                messages, tools=tools, model=model, base_url=base_url, api_key=api_key
            )
            try:
                first = await asyncio.wait_for(anext(gen), timeout=self.timeout_s)
            except BaseException as exc:
                mapped = classify(exc)
                if mapped is not exc:
                    await gen.aclose()
                    raise mapped from exc
                raise
            self._gen = gen
            return first

        first = await self._retry(open_stream)
        yield first
        async for evt in self._gen:
            yield evt
