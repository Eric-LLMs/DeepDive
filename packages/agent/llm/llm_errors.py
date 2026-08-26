"""LLM error taxonomy: temporary (retryable) vs fatal (surface to the caller).

The agent's LLM calls go through :class:`~agent.llm.llm_guard.ReliableLLM`, which retries
temporary errors (timeout, rate limit, server/connection hiccups) with backoff and lets
fatal errors (auth, bad request) surface as :class:`LLMFatalError`. ``classify`` maps any
exception to the right bucket without hard-coding a specific SDK, so the wrapper works with
any :class:`~agent.engine.loop.AgentLLMPort` implementation (tests use a fake port).
"""
from __future__ import annotations

import asyncio
from typing import NoReturn


class LLMTemporaryError(asyncio.TimeoutError):
    """A retryable failure: timeout, 429, 5xx, or a connection-level hiccup."""


class LLMFatalError(RuntimeError):
    """A non-retryable failure: auth, bad request, or an unknown error."""


def classify(exc: BaseException) -> BaseException:
    """Map ``exc`` to :class:`LLMTemporaryError` / :class:`LLMFatalError`, or pass through.

    Base-exception control flow (``CancelledError`` / ``KeyboardInterrupt`` / ``SystemExit``)
    is returned unchanged — it must never be caught by a retry loop.
    """
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return exc
    if isinstance(exc, (LLMTemporaryError, LLMFatalError)):
        return exc
    if isinstance(exc, asyncio.TimeoutError):
        return LLMTemporaryError(str(exc))

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if 500 <= status < 600:
            return LLMTemporaryError(f"llm server error ({status}): {exc}")
        return LLMFatalError(f"llm client error ({status}): {exc}")

    # Connection-level errors (openai APIConnectionError / APITimeoutError / RateLimitError
    # / InternalServerError) are retryable; anything else is treated as fatal.
    name = type(exc).__name__
    if name in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"}:
        return LLMTemporaryError(f"llm connection error: {exc}")

    return LLMFatalError(f"llm error: {exc}")


def raise_classified(exc: BaseException) -> NoReturn:
    """Raise ``exc`` after :func:`classify` (control-flow exceptions re-raised unchanged)."""
    mapped = classify(exc)
    raise mapped
