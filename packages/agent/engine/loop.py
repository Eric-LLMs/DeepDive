"""Agent main loop: a ReactLoopAgent step pipeline delegating tool lifecycle to the ToolRuntime.

The loop stays small and testable; the tool lifecycle (pre/execute/post/result) is a single
:meth:`ToolRuntime.execute` call. Each step assembles the :class:`SystemPrompt`, runs one LLM
call, then executes any tool calls — concurrency-safe tools in parallel, others as serial
barriers. Session-level hooks (``agent/session-start`` / ``agent/session-end``) are the loop's
own extension points, mirroring the agent session lifecycle.

**Per-turn isolation** — every piece of mutable state (recall hits, injected content, usage,
cancellation, budget) lives on the :class:`AgentTurn` bound for the duration of the run, so
concurrent turns on the shared kernel/loop never race. When no turn is passed, the loop builds
one from the legacy positional arguments (kept for backward compatibility).

**Reliability** — the LLM port is expected to be a :class:`~agent.llm.llm_guard.ReliableLLM`
(timeout + retry + cancellation). A fatal LLM error surfaces as a graceful ``AgentResult.error``
(never a hang); ``CancelledError`` (SSE disconnect) is logged and re-raised after session
cleanup; tool failures are isolated per child (one failure never aborts healthy siblings); the
message window is bounded in-loop; a hard per-turn budget aborts the loop once exhausted.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.config import settings

from agent.engine.context import AgentTurn, bind_turn
from agent.engine.decisions import (
    ToolExecution,
    ToolExecutionFailure,
    ToolExecutionResult,
    ToolFailure,
)
from agent.engine.loop_guard import ToolLoopTracker
from agent.engine.runtime import ToolRuntime
from agent.engine.sessions import SessionLog
from agent.engine.telemetry import TraceContext, TurnSpan, estimate_cost_usd, log_error, log_event
from agent.llm.llm_errors import LLMFatalError, LLMTemporaryError
from agent.prompt.system_prompt import (
    CacheBoundaryAssembler,
    PromptAssembly,
    SystemPrompt,
    render_prompt,
)
from agent.tools.tool_gateway import ToolGateway


class AgentLLMPort(Protocol):
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> dict:
        """Return {"content": str | None, "tool_calls": [{id, name, arguments}]}."""
        ...

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[dict]:
        """Optional streaming variant of :meth:`chat`.

        Yields event dicts ``{"type": "thinking"|"content", "data": <delta>}`` per chunk,
        then a final ``{"type": "tool_calls", "data": [...]}`` event. The agent loop's
        :meth:`ReactLoopAgent.run_stream` consumes these; a port that only implements
        ``chat`` still works for the non-streaming :meth:`run`.
        """
        ...


@dataclass
class AgentResult:
    messages: list[dict]
    final_answer: str
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    cost_usd: float = 0.0


class ReactLoopAgent:
    def __init__(
        self,
        llm: AgentLLMPort,
        runtime: ToolRuntime,
        system_prompt: SystemPrompt,
        session_log: SessionLog | None = None,
        max_steps: int = 5,
        max_parallel_tool_calls: int = 10,
        gateway: ToolGateway | None = None,
    ) -> None:
        self.llm = llm
        self.runtime = runtime
        self.system_prompt = system_prompt
        self.session_log = session_log
        self.max_steps = max_steps
        self.max_parallel_tool_calls = max_parallel_tool_calls
        self.gateway = gateway
        self.events = runtime.events

    def _log(self, turn: AgentTurn, type_: str, **payload: Any) -> None:
        if self.session_log is not None:
            self.session_log.append(type_, **payload)
        if turn.session_memory is not None:
            turn.session_memory.record_event(type_, payload)
        if turn.audit is not None:
            turn.audit.write({**TraceContext.snapshot(), "type": type_, **payload})
        log_event(type_, **payload)

    # ── turn lifecycle ──
    def _ensure_turn(
        self,
        user_msg: str,
        history: list[dict] | None,
        memory_keys: list[str] | None,
        session_memory: Any | None,
        model: str | None,
        base_url: str | None,
        api_key: str | None,
        turn: AgentTurn | None,
    ) -> AgentTurn:
        if turn is None:
            turn = AgentTurn(
                user_msg=user_msg,
                history=history,
                memory_keys=memory_keys,
                session_memory=session_memory,
                model=model,
                base_url=base_url,
                api_key=api_key,
                max_budget_usd=settings.max_budget_per_turn_usd,
            )
        elif session_memory is not None:
            turn.session_memory = session_memory
        bind_turn(turn)  # idempotent: the kernel binds first; direct callers get bound here
        if turn.loop_tracker is None:
            turn.loop_tracker = ToolLoopTracker()  # tool-oscillation breaker, on by default
        if turn.span is None:
            turn.span = TurnSpan(turn.turn_id)
        TraceContext.bind(turn_id=turn.turn_id)
        return turn

    def _turn_context(self, turn: AgentTurn) -> dict:
        return {
            "user_msg": turn.user_msg,
            "history": turn.history,
            "memory_keys": turn.memory_keys,
            "session_memory": turn.session_memory,
            "turn": turn,
        }

    async def run(
        self,
        user_msg: str,
        history: list[dict] | None = None,
        memory_keys: list[str] | None = None,
        session_memory: Any | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        turn: AgentTurn | None = None,
        tools: list[dict] | None = None,
        max_steps: int | None = None,
        progress_sink: Callable[[dict], None] | None = None,
    ) -> AgentResult:
        """Run one turn: assemble prompt → step until a final answer, budget, or max_steps.

        ``tools`` overrides the model-visible tool schemas (used by subagents to pass a
        filtered set); ``max_steps`` overrides the loop cap; ``progress_sink`` receives
        structured turn events (plan / tool progress) for client streaming.
        """
        turn = self._ensure_turn(
            user_msg, history, memory_keys, session_memory, model, base_url, api_key, turn
        )
        if progress_sink is not None:
            turn.progress_sink = progress_sink
        model = model or turn.model
        base_url = base_url or turn.base_url
        api_key = api_key or turn.api_key
        step_cap = max_steps if max_steps is not None else self.max_steps

        if self.gateway is not None:
            self.gateway.reset_session()

        context = self._turn_context(turn)
        assembly = await self.system_prompt.assemble(context)
        system = render_prompt(assembly)

        await self.events.serial("agent/session-start", {"user_msg": turn.user_msg})
        self._log(turn, "session-start", user_msg=turn.user_msg, snapshot_key=self._snapshot_key())

        messages = (turn.history or []) + [{"role": "user", "content": turn.user_msg}]
        await self._persist_message(turn.session_memory, "user", turn.user_msg)

        error: str | None = None
        try:
            for step in range(step_cap):
                if turn.cancel_token.is_set():
                    break
                await self.events.serial("agent/step-start", {})
                self._log(turn, "step-start")
                if isinstance(self.system_prompt, CacheBoundaryAssembler):
                    dynamic = await self.system_prompt.refresh_dynamic(context)
                    if dynamic != assembly.dynamic_suffix:
                        assembly.dynamic_suffix = dynamic
                        system = render_prompt(assembly)
                visible_tools = self._step_tools(context, assembly, tools)
                t0 = time.monotonic()
                try:
                    finished, step_usage = await self._step(
                        turn, system, visible_tools, messages, step, model, base_url, api_key
                    )
                except (LLMFatalError, LLMTemporaryError) as exc:
                    error = str(exc)
                    log_error(kind="llm_fatal", message=error)
                    turn.span.record_error(kind="llm_fatal", message=error)
                    break
                turn.add_usage(step_usage)
                turn.span.record_step(
                    index=step,
                    tool_calls=0,
                    tokens=turn.usage.get("total_tokens", 0),
                    duration_ms=(time.monotonic() - t0) * 1000,
                )
                self._enforce_window(messages)
                await self.events.serial("agent/step-end", {})
                self._log(turn, "step-end")
                if self._budget_exceeded(turn, model):
                    break
                if self._oscillation(turn):
                    messages.append(self._oscillation_guidance())
                    self._reset_oscillation(turn)
                if finished:
                    break
        except asyncio.CancelledError:
            log_error(kind="turn_cancelled")
            turn.span.record_error(kind="cancelled", message="turn cancelled")
            raise
        finally:
            await self.events.serial("agent/session-end", {"messages": messages})
            self._log(turn, "session-end")
            if turn.session_memory is not None:
                await turn.session_memory.close()
            turn.span.finish(cost_usd=estimate_cost_usd(turn.usage, model))
            if turn.audit is not None:
                turn.audit.write(
                    {**TraceContext.snapshot(), "type": "turn-end", **turn.span.to_dict()}
                )

        final_answer = self._final(messages) if error is None else (
            "I ran into a model error and couldn't answer this turn. "
            f"({error})"
        )
        return AgentResult(
            messages=messages,
            final_answer=final_answer,
            usage=turn.usage,
            error=error,
            cost_usd=turn.span.cost_usd,
        )

    async def run_stream(
        self,
        user_msg: str,
        history: list[dict] | None = None,
        memory_keys: list[str] | None = None,
        session_memory: Any | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        turn: AgentTurn | None = None,
        tools: list[dict] | None = None,
        max_steps: int | None = None,
        progress_sink: Callable[[dict], None] | None = None,
    ) -> AsyncIterator[dict]:
        """Streaming variant of :meth:`run` (same overrides as :meth:`run`).

        Yields per-step events: ``thinking`` / ``content`` deltas, ``tool`` (about to
        dispatch), ``step-answer``, then ``done``. A fatal LLM error yields an ``error``
        event before ``done``. If the generator is abandoned (client disconnect) the
        ``finally`` block still closes the session memory and the turn span.
        """
        turn = self._ensure_turn(
            user_msg, history, memory_keys, session_memory, model, base_url, api_key, turn
        )
        if progress_sink is not None:
            turn.progress_sink = progress_sink
        model = model or turn.model
        base_url = base_url or turn.base_url
        api_key = api_key or turn.api_key
        step_cap = max_steps if max_steps is not None else self.max_steps

        if self.gateway is not None:
            self.gateway.reset_session()

        context = self._turn_context(turn)
        assembly = await self.system_prompt.assemble(context)
        system = render_prompt(assembly)

        await self.events.serial("agent/session-start", {"user_msg": turn.user_msg})
        self._log(turn, "session-start", user_msg=turn.user_msg, snapshot_key=self._snapshot_key())

        messages = (turn.history or []) + [{"role": "user", "content": turn.user_msg}]
        await self._persist_message(turn.session_memory, "user", turn.user_msg)

        error: str | None = None
        try:
            for step in range(step_cap):
                if turn.cancel_token.is_set():
                    break
                await self.events.serial("agent/step-start", {})
                self._log(turn, "step-start")
                if isinstance(self.system_prompt, CacheBoundaryAssembler):
                    dynamic = await self.system_prompt.refresh_dynamic(context)
                    if dynamic != assembly.dynamic_suffix:
                        assembly.dynamic_suffix = dynamic
                        system = render_prompt(assembly)
                visible_tools = self._step_tools(context, assembly, tools)

                request = [{"role": "system", "content": system}] + self._snip_messages(messages)
                content = ""
                tool_calls: list[dict] = []
                step_usage: dict[str, int] | None = None
                t0 = time.monotonic()
                try:
                    async for evt in self.llm.chat_stream(
                        request, tools=visible_tools, model=model, base_url=base_url, api_key=api_key
                    ):
                        kind = evt.get("type")
                        if kind == "thinking" and evt.get("data"):
                            yield {"type": "thinking", "data": evt["data"]}
                        elif kind == "content" and evt.get("data"):
                            content += evt["data"]
                            yield {"type": "content", "data": evt["data"]}
                        elif kind == "tool_calls":
                            tool_calls = evt.get("data") or []
                        elif kind == "usage":
                            step_usage = evt.get("data")
                except (LLMFatalError, LLMTemporaryError) as exc:
                    error = str(exc)
                    log_error(kind="llm_fatal", message=error)
                    turn.span.record_error(kind="llm_fatal", message=error)
                    yield {"type": "error", "data": {"message": error}}
                    break

                turn.add_usage(step_usage)
                self._log(turn, "llm-call", tool_calls=len(tool_calls))
                turn.span.record_llm(duration_ms=(time.monotonic() - t0) * 1000)

                assistant: dict = {"role": "assistant", "content": content or None}
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                messages.append(assistant)
                if content:
                    await self._persist_message(turn.session_memory, "assistant", content)
                    yield {"type": "step-answer", "data": content}

                await self.events.serial("agent/step-end", {})
                self._log(turn, "step-end")

                if not tool_calls:
                    break
                for tc in tool_calls:
                    yield {"type": "tool", "data": {"name": tc["name"]}}
                concludes = await self._execute_tool_calls(turn, tool_calls, messages, model)
                self._enforce_window(messages)
                if self._budget_exceeded(turn, model):
                    yield {"type": "error", "data": {"message": "per-turn budget exceeded"}}
                    break
                if self._oscillation(turn):
                    messages.append(self._oscillation_guidance())
                    self._reset_oscillation(turn)
                if concludes:
                    break
        except asyncio.CancelledError:
            log_error(kind="turn_cancelled")
            turn.span.record_error(kind="cancelled", message="turn cancelled")
            raise
        finally:
            await self.events.serial("agent/session-end", {"messages": messages})
            self._log(turn, "session-end")
            if turn.session_memory is not None:
                await turn.session_memory.close()
            turn.span.finish(cost_usd=estimate_cost_usd(turn.usage, model))
            if turn.audit is not None:
                turn.audit.write(
                    {**TraceContext.snapshot(), "type": "turn-end", **turn.span.to_dict()}
                )

        answer = self._final(messages) if error is None else (
            "I ran into a model error and couldn't answer this turn. "
            f"({error})"
        )
        yield {
            "type": "done",
            "data": {
                "answer": answer,
                "messages": messages,
                "usage": turn.usage,
                "error": error,
                "cost_usd": turn.span.cost_usd,
            },
        }

    # ── guards ──
    @staticmethod
    def _budget_exceeded(turn: AgentTurn, model: str | None) -> bool:
        """Hard per-turn budget: abort the loop once accumulated cost passes the cap."""
        if not turn.max_budget_usd:
            return False
        cost = estimate_cost_usd(turn.usage, model)
        return cost >= turn.max_budget_usd

    def _oscillation(self, turn: AgentTurn) -> bool:
        return bool(turn.loop_tracker is not None and turn.loop_tracker.should_break())

    @staticmethod
    def _reset_oscillation(turn: AgentTurn) -> None:
        if turn.loop_tracker is not None:
            turn.loop_tracker.reset()

    @staticmethod
    def _oscillation_guidance() -> dict:
        return {
            "role": "user",
            "content": (
                "[system] You have repeatedly performed the same tool call or repeated the "
                "same failing operation. Do NOT repeat it again. Change your approach, or "
                "explain the blocker to the user and ask for guidance."
            ),
        }

    def _enforce_window(self, messages: list[dict]) -> None:
        """Bound the in-memory message window: drop the oldest tool results when over budget.

        User/assistant turns are always kept; only *tool* results older than needed are
        trimmed (newest first), so the request stays within ``prompt_max_chars``.
        """
        budget = settings.prompt_max_chars
        if budget <= 0:
            return
        if sum(len(m.get("content") or "") for m in messages) <= budget:
            return
        others = [m for m in messages if m.get("role") != "tool"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        avail = budget - sum(len(m.get("content") or "") for m in others)
        kept: list[dict] = []
        for m in reversed(tool_msgs):  # newest first
            if avail <= 0:
                break
            kept.append(m)
            avail -= len(m.get("content") or "")
        messages[:] = others + list(reversed(kept))

    # ── helpers ──
    def _runtime_schemas(self) -> list[dict]:
        return [{"type": "function", "function": s} for s in self.runtime.schemas()]

    def _snapshot_key(self) -> str:
        if isinstance(self.system_prompt, CacheBoundaryAssembler):
            return self.system_prompt.snapshot_key()
        return ""

    def _step_tools(
        self, context: dict, assembly: PromptAssembly, tools: list[dict] | None = None
    ) -> list[dict]:
        """The model-visible tools for this step (explicit override wins).

        ``tools`` lets a caller (e.g. a subagent) pass a filtered full-schema set instead of
        the gateway's deferred-loading projection.
        """
        if tools is not None:
            return tools
        if self.gateway is not None:
            return self.gateway.visible_schemas(context)
        return assembly.tools or self._runtime_schemas()

    async def _step(
        self,
        turn: AgentTurn,
        system: str,
        tools: list[dict],
        messages: list[dict],
        step: int,
        model: str | None,
        base_url: str | None,
        api_key: str | None,
    ) -> tuple[bool, dict | None]:
        """Run one LLM call + execute any tool calls. Returns ``(finished, usage)``."""
        request = [{"role": "system", "content": system}] + self._snip_messages(messages)
        resp = await self.llm.chat(request, tools=tools, model=model, base_url=base_url, api_key=api_key)
        tool_calls = resp.get("tool_calls") or []
        usage = resp.get("usage")
        self._log(turn, "llm-call", tool_calls=len(tool_calls))

        assistant: dict = {"role": "assistant", "content": resp["content"]}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        messages.append(assistant)
        if resp["content"]:
            await self._persist_message(turn.session_memory, "assistant", resp["content"])

        if not tool_calls:
            return True, usage

        concludes = await self._execute_tool_calls(turn, tool_calls, messages, model)
        return concludes, usage

    async def _execute_tool_calls(
        self, turn: AgentTurn, tool_calls: list[dict], messages: list[dict], model: str | None
    ) -> bool:
        """Execute tool calls, grouping concurrency-safe tools in parallel (serial barriers)."""
        i = 0
        concludes = False
        while i < len(tool_calls):
            tc = tool_calls[i]
            if self._is_concurrency_safe(tc["name"]):
                group: list[dict] = []
                while (
                    i < len(tool_calls)
                    and len(group) < self.max_parallel_tool_calls
                    and self._is_concurrency_safe(tool_calls[i]["name"])
                ):
                    group.append(tool_calls[i])
                    i += 1
                dispatched = await asyncio.gather(
                    *(self._dispatch(turn, tc, model) for tc in group), return_exceptions=True
                )
                for tc_, dispatched_ in zip(group, dispatched):
                    if isinstance(dispatched_, BaseException):
                        # BaseException (e.g. CancelledError) propagates; ordinary Exceptions
                        # become a tool failure so healthy siblings still commit in order.
                        if isinstance(dispatched_, Exception):
                            dispatched_ = (tc_, self._error_exec(tc_), self._error_result(dispatched_))
                        else:
                            raise dispatched_
                    exec_, result = dispatched_[1], dispatched_[2]
                    self._commit_tool(messages, tc_, exec_, result)
                    concludes = concludes or exec_.concludes_turn
            else:
                tc_, exec_, result = await self._dispatch(turn, tc, model)
                self._commit_tool(messages, tc_, exec_, result)
                concludes = concludes or exec_.concludes_turn
                i += 1
            if concludes:
                break
        return concludes

    @staticmethod
    def _error_exec(tc: dict) -> ToolExecution:
        return ToolExecution(call_id=tc["id"], name=tc["name"], arguments={})

    @staticmethod
    def _error_result(exc: Exception) -> ToolExecutionFailure:
        return ToolExecutionFailure(ToolFailure(str(exc), info={"name": "tool_error"}))

    def _is_concurrency_safe(self, name: str) -> bool:
        tool = self.runtime.get(name)
        return tool is not None and tool.is_concurrency_safe is True

    async def _dispatch(
        self, turn: AgentTurn, tc: dict, model: str | None
    ) -> tuple[dict, ToolExecution, ToolExecutionResult]:
        args = json.loads(tc["arguments"] or "{}")
        exec_ = ToolExecution(call_id=tc["id"], name=tc["name"], arguments=args, agent=self)
        if turn.loop_tracker is not None:
            turn.loop_tracker.record(tc["name"], args)
        turn.emit({"type": "tool-start", "data": {"name": tc["name"], "call_id": tc["id"]}})
        self._log(turn, "tool-call", name=tc["name"], call_id=tc["id"])
        t0 = time.monotonic()
        result = await self.runtime.execute(exec_)
        if turn.loop_tracker is not None and result.is_error:
            turn.loop_tracker.record_error(tc["name"], result.error.message)
        turn.span.record_tool(
            name=tc["name"], is_error=result.is_error, duration_ms=(time.monotonic() - t0) * 1000
        )
        self._log(turn, "tool-result", name=tc["name"], is_error=result.is_error)
        turn.emit(
            {"type": "tool-result", "data": {"name": tc["name"], "call_id": tc["id"], "is_error": result.is_error}}
        )
        return tc, exec_, result

    def _commit_tool(
        self, messages: list[dict], tc: dict, exec_: ToolExecution, result: ToolExecutionResult
    ) -> None:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": self._tool_content(result),
            }
        )
        messages.extend(exec_.deferred_contexts)

    async def _persist_message(self, session_memory: Any | None, role: str, text: str) -> None:
        if session_memory is not None and text:
            await session_memory.append_message(role, text)

    @staticmethod
    def _snip(content: str) -> str:
        cap = settings.prompt_message_max_chars
        if cap > 0 and len(content) > cap:
            return content[: cap - 1].rstrip() + "…(truncated)"
        return content

    @classmethod
    def _snip_messages(cls, messages: list[dict]) -> list[dict]:
        return [
            {**m, "content": cls._snip(m["content"])}
            if isinstance(m.get("content"), str)
            else m
            for m in messages
        ]

    @staticmethod
    def _tool_content(result: ToolExecutionResult) -> str:
        if result.is_error:
            return json.dumps({"error": result.error.message}, ensure_ascii=False)
        if result.content:
            return "\n".join(b.text for b in result.content if b.type == "text")
        return json.dumps(result.value, ensure_ascii=False, default=str)

    @staticmethod
    def _final(messages: list[dict]) -> str:
        for m in reversed(messages):
            if m["role"] == "assistant" and m.get("content"):
                return m["content"]
        return ""
