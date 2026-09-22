"""DirectExecutor: the Phase 2 fast path — a single, tool-less model call.

Serves turns the policy resolved to ``PlanKind.DIRECT``: short, pure, no capability
demand (no private corpus, no web, no viewer, no action, no memory recall). Because
there is nothing to fetch and no ReAct loop to drive, the only work is one streamed
completion — which is exactly the latency the refactor set out to reclaim.

What this branch is NOT allowed to do, and why it can still reuse every invariant:
  * it emits NO ``tool_calls`` (``tools=None``), so the approval pump and the tool
    runtime are never engaged — a direct turn cannot mutate state or cross an ACL;
  * it still owns message PERSISTENCE: it appends the user + assistant rows to
    ``ctx.session_memory`` exactly like the loop does, so the transcript, the
    session-finalize job and the usage metering in :mod:`core.application.chat.lifecycle`
    behave identically to the Agent path;
  * on an LLM error it yields an ``error`` event then a ``done`` with ``error`` set,
    mirroring the loop's terminal shape — the client sees the same SSE contract.

It reuses the kernel's RELIABILITY-WRAPPED port (``agent.loop.llm``), so the hard
timeout, tenacity retry and cancellation pass-through are identical to the agent path
— the fast path must be faster, never less dependable.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from core.application.chat.execution_plan import PlanKind
from core.application.chat.executors.base import ChatEvent, ProgressSink, TurnRequest
from core.config import settings

# A deliberately small, tool-free persona. The direct path has no capabilities to
# describe, so the system prompt stays short — a large prompt would erase the very
# latency budget this branch exists to save. The model is told not to fabricate
# facts it cannot know, since there is no retrieval behind it.
DIRECT_SYSTEM = (
    "You are Delveta, a concise, knowledgeable study-and-work assistant. Answer the "
    "user's request directly and helpfully. You have no tools in this mode: if the "
    "answer depends on the user's private documents, live/current data, or an action, "
    "say so briefly instead of inventing it. Match the user's language."
)


@dataclass
class DirectResult:
    """The non-streaming result, shaped like the loop's ``AgentResult`` so lifecycle
    bookkeeping (finalize_turn reads ``answer``/``usage``/``messages``) is uniform."""

    messages: list[dict]
    final_answer: str
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    cost_usd: float | None = None


def _snip(text: str) -> str:
    cap = settings.prompt_message_max_chars
    if cap > 0 and len(text) > cap:
        return text[: cap - 1].rstrip() + "…(truncated)"
    return text


class DirectExecutor:
    kind = PlanKind.DIRECT

    # Overridable seam: a subclass (e.g. the viewer branch) prepends its grounded
    # reference section to the system prompt while reusing the entire single-shot
    # stream/run machinery below verbatim.
    def system_prompt(self, req: TurnRequest) -> str:
        return DIRECT_SYSTEM

    def _build_request(self, req: TurnRequest, system: str | None = None) -> list[dict]:
        """Assemble the single-shot request: system + snipped history + this user turn.

        ``system`` overrides :meth:`system_prompt` — the staged retrieval branch passes
        a per-turn grounded prompt (evidence is only known at execution time).
        """
        ctx = req.ctx
        history = [
            {"role": m["role"], "content": _snip(m["content"])}
            for m in (ctx.history or [])
            if isinstance(m.get("content"), str) and m.get("role") in ("user", "assistant")
        ]
        return [
            {"role": "system", "content": system if system is not None else self.system_prompt(req)},
            *history,
            {"role": "user", "content": ctx.user_text},
        ]

    @staticmethod
    def _llm(req: TurnRequest):
        # The kernel's reliability-wrapped port (timeout + retry + cancel), not the raw
        # transport — the direct path inherits the agent path's dependability for free.
        return req.deps.agent.loop.llm

    async def stream(
        self, req: TurnRequest, *, progress_sink: ProgressSink
    ) -> AsyncIterator[ChatEvent]:
        async for evt in self._stream_request(req, self._build_request(req)):
            yield evt

    async def _stream_request(
        self, req: TurnRequest, request: list[dict]
    ) -> AsyncIterator[ChatEvent]:
        """The single-shot stream machinery over an ASSEMBLED request.

        Split out so a staged subclass (retrieval) can run its pre-flight stages and
        then reuse this exact persistence / SSE / error path — one terminal shape for
        every tool-less branch.
        """
        ctx = req.ctx
        messages: list[dict] = request[1:]  # history + user (system excluded from echo)
        await ctx.session_memory.append_message("user", ctx.user_text)

        content = ""
        usage: dict[str, int] | None = None
        error: str | None = None
        from agent.llm.llm_errors import LLMFatalError, LLMTemporaryError

        try:
            async for evt in self._llm(req).chat_stream(
                request, tools=None,
                model=ctx.model, base_url=ctx.base_url or None, api_key=ctx.api_key or None,
                disable_thinking=ctx.disable_thinking,
            ):
                kind = evt.get("type")
                if kind == "thinking" and evt.get("data"):
                    yield {"type": "thinking", "data": evt["data"]}
                elif kind == "content" and evt.get("data"):
                    content += evt["data"]
                    yield {"type": "content", "data": evt["data"]}
                elif kind == "usage":
                    usage = evt.get("data")
        except (LLMFatalError, LLMTemporaryError) as exc:
            error = str(exc)
            yield {"type": "error", "data": {"message": error}}

        answer = content
        # On error, mirror the loop: it breaks before committing the step, so no partial
        # assistant row is written and the transcript carries only the user turn.
        if answer and error is None:
            messages.append({"role": "assistant", "content": answer})
            await ctx.session_memory.append_message("assistant", answer)
        else:
            messages.append({"role": "assistant", "content": None})
        yield {
            "type": "done",
            "data": {
                "answer": answer,
                "messages": messages,
                "usage": usage or {},
                "error": error,
                "cost_usd": None,  # single-shot cost is derived downstream from usage
            },
        }

    async def run(self, req: TurnRequest) -> DirectResult:
        return await self._run_request(req, self._build_request(req))

    async def _run_request(self, req: TurnRequest, request: list[dict]) -> DirectResult:
        """The non-streaming single-shot machinery over an ASSEMBLED request."""
        ctx = req.ctx
        messages: list[dict] = request[1:]
        await ctx.session_memory.append_message("user", ctx.user_text)

        from agent.llm.llm_errors import LLMFatalError, LLMTemporaryError

        error: str | None = None
        content = ""
        usage: dict[str, int] = {}
        try:
            resp = await self._llm(req).chat(
                request, tools=None,
                model=ctx.model, base_url=ctx.base_url or None, api_key=ctx.api_key or None,
            )
            content = resp.get("content") or ""
            usage = resp.get("usage") or {}
        except (LLMFatalError, LLMTemporaryError) as exc:
            error = str(exc)
            content = ""

        messages = [*messages, {"role": "assistant", "content": content or None}]
        if content:
            await ctx.session_memory.append_message("assistant", content)
        return DirectResult(
            messages=messages, final_answer=content, usage=usage, error=error,
        )
