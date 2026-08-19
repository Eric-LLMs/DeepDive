"""Agent main loop: a ReactLoopAgent step pipeline delegating tool lifecycle to the ToolRuntime.

The loop stays small and testable; the tool lifecycle (pre/execute/post/result) is a single
:meth:`ToolRuntime.execute` call. Each step assembles the :class:`SystemPrompt`, runs one LLM
call, then executes any tool calls — concurrency-safe tools in parallel, others as serial
barriers. Session-level hooks (``agent/session-start`` / ``agent/session-end``) are the loop's
own extension points, mirroring the agent session lifecycle.

When a :class:`SessionLog` is supplied, every step is recorded as an append-only event; when a
persistent session memory is supplied, messages/events are persisted and closed on session end.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.decisions import ToolExecution, ToolExecutionResult
from agent.runtime import ToolRuntime
from agent.sessions import SessionLog
from agent.system_prompt import CacheBoundaryAssembler, PromptAssembly, SystemPrompt, render_prompt
from agent.tool_gateway import ToolGateway


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


@dataclass
class AgentResult:
    messages: list[dict]
    final_answer: str
    usage: dict[str, int] = field(default_factory=dict)


def _sum_usage(total: dict[str, int], step: dict | None) -> dict[str, int]:
    """Accumulate per-call token counts into a running total."""
    if not step:
        return total
    out = dict(total)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        out[key] = out.get(key, 0) + int(step.get(key) or 0)
    return out


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
        self._session_memory: Any | None = None

    def _log(self, type_: str, **payload: Any) -> None:
        if self.session_log is not None:
            self.session_log.append(type_, **payload)
        if self._session_memory is not None:
            self._session_memory.record_event(type_, payload)

    async def run(
        self,
        user_msg: str,
        history: list[dict] | None = None,
        memory_keys: list[str] | None = None,
        session_memory: Any | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> AgentResult:
        """Run one turn: assemble prompt → step until a final answer or max_steps.

        With a :class:`CacheBoundaryAssembler` the static/project head is assembled once
        and only the dynamic suffix is re-rendered per step (reused when unchanged);
        with a :class:`ToolGateway` the model-visible tool schemas are recomputed per step
        (core + mounted, so deferred ``tool_search`` loads appear on the next step).

        ``base_url`` / ``api_key`` route every model call in this turn through a specific
        LLM channel (the credential pinned on the caller's access token); ``None`` keeps
        the shared, config-driven client.
        """
        self._session_memory = session_memory
        context = {
            "user_msg": user_msg,
            "history": history,
            "memory_keys": memory_keys,
            "session_memory": session_memory,
        }
        if isinstance(self.system_prompt, CacheBoundaryAssembler):
            self.system_prompt.begin_session()
        if self.gateway is not None:
            self.gateway.reset_session()

        assembly = await self.system_prompt.assemble(context)
        system = render_prompt(assembly)

        await self.events.serial("agent/session-start", {"user_msg": user_msg})
        self._log("session-start", user_msg=user_msg, snapshot_key=self._snapshot_key())

        messages = (history or []) + [{"role": "user", "content": user_msg}]
        await self._persist_message(session_memory, "user", user_msg)

        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        try:
            for _ in range(self.max_steps):
                await self.events.serial("agent/step-start", {})
                self._log("step-start")
                if isinstance(self.system_prompt, CacheBoundaryAssembler):
                    dynamic = await self.system_prompt.refresh_dynamic(context)
                    if dynamic != assembly.dynamic_suffix:
                        assembly.dynamic_suffix = dynamic
                        system = render_prompt(assembly)
                tools = self._step_tools(context, assembly)
                finished, step_usage = await self._step(
                    system, tools, messages, session_memory, model, base_url, api_key
                )
                usage = _sum_usage(usage, step_usage)
                await self.events.serial("agent/step-end", {})
                self._log("step-end")
                if finished:
                    break
        finally:
            await self.events.serial("agent/session-end", {"messages": messages})
            self._log("session-end")
            if session_memory is not None:
                await session_memory.close()
            self._session_memory = None

        return AgentResult(messages=messages, final_answer=self._final(messages), usage=usage)

    def _runtime_schemas(self) -> list[dict]:
        return [{"type": "function", "function": s} for s in self.runtime.schemas()]

    def _snapshot_key(self) -> str:
        """Prefix-cache identity: sha256(static + project), stable across steps."""
        if isinstance(self.system_prompt, CacheBoundaryAssembler):
            return self.system_prompt.snapshot_key()
        return ""

    def _step_tools(self, context: dict, assembly: PromptAssembly) -> list[dict]:
        """Model-visible tool schemas: gateway (core + mounted) when present, else fallback."""
        if self.gateway is not None:
            return self.gateway.visible_schemas(context)
        return assembly.tools or self._runtime_schemas()

    async def _step(
        self,
        system: str,
        tools: list[dict],
        messages: list[dict],
        session_memory: Any | None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> tuple[bool, dict | None]:
        """Run one LLM call + execute any tool calls.

        Returns ``(finished, usage)`` where ``usage`` is the token counts from this step's
        LLM call (``None`` when the port reported none).
        """
        request = [{"role": "system", "content": system}] + messages
        resp = await self.llm.chat(request, tools=tools, model=model, base_url=base_url, api_key=api_key)
        tool_calls = resp.get("tool_calls") or []
        usage = resp.get("usage")
        self._log("llm-call", tool_calls=len(tool_calls))

        assistant: dict = {"role": "assistant", "content": resp["content"]}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        messages.append(assistant)
        if resp["content"]:
            await self._persist_message(session_memory, "assistant", resp["content"])

        if not tool_calls:
            return True, usage

        concludes = await self._execute_tool_calls(tool_calls, messages, session_memory)
        return concludes, usage

    async def _execute_tool_calls(
        self, tool_calls: list[dict], messages: list[dict], session_memory: Any | None
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
                dispatched = await asyncio.gather(*(self._dispatch(tc) for tc in group))
                for tc_, exec_, result in dispatched:  # commit in model order
                    self._commit_tool(messages, tc_, exec_, result)
                    concludes = concludes or exec_.concludes_turn
            else:
                tc_, exec_, result = await self._dispatch(tc)
                self._commit_tool(messages, tc_, exec_, result)
                concludes = concludes or exec_.concludes_turn
                i += 1
            if concludes:
                break
        return concludes

    def _is_concurrency_safe(self, name: str) -> bool:
        tool = self.runtime.get(name)
        return tool is not None and tool.is_concurrency_safe is True

    async def _dispatch(
        self, tc: dict
    ) -> tuple[dict, ToolExecution, ToolExecutionResult]:
        args = json.loads(tc["arguments"] or "{}")
        exec_ = ToolExecution(call_id=tc["id"], name=tc["name"], arguments=args, agent=self)
        self._log("tool-call", name=tc["name"], call_id=tc["id"])
        result = await self.runtime.execute(exec_)
        self._log("tool-result", name=tc["name"], is_error=result.is_error)
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
