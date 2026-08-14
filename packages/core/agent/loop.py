"""Agent main loop: an explicit while loop delegating tool lifecycle to the ToolRuntime.

The loop stays small and testable; the tool lifecycle (pre/execute/post/result) is a single
:meth:`ToolRuntime.execute` call. Session-level hooks (``agent/session-start`` /
``agent/session-end``) are the loop's own extension points, mirroring the agent session
lifecycle.
"""
import json
from dataclasses import dataclass
from typing import Protocol

from core.agent.context import ContextBuilder
from core.agent.decisions import ToolExecution, ToolExecutionResult
from core.agent.runtime import ToolRuntime


class AgentLLMPort(Protocol):
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Return {"content": str | None, "tool_calls": [{id, name, arguments}]}."""
        ...


@dataclass
class AgentResult:
    messages: list[dict]
    final_answer: str


class Agent:
    def __init__(
        self,
        llm: AgentLLMPort,
        runtime: ToolRuntime,
        context: ContextBuilder | None = None,
        max_steps: int = 5,
    ) -> None:
        self.llm = llm
        self.runtime = runtime
        self.context = context
        self.max_steps = max_steps
        self.events = runtime.events

    async def run(
        self,
        user_msg: str,
        history: list[dict] | None = None,
        memory_keys: list[str] | None = None,
    ) -> AgentResult:
        """Run one turn of conversation, returns the full message list + the final answer."""
        if self.context:
            messages = await self.context.build(user_msg, history, memory_keys)
        else:
            messages = (history or []) + [{"role": "user", "content": user_msg}]

        tools = [{"type": "function", "function": s} for s in self.runtime.schemas()]
        await self.events.serial("agent/session-start", {"messages": messages})

        for _ in range(self.max_steps):
            resp = await self.llm.chat(messages, tools=tools)

            assistant: dict = {"role": "assistant", "content": resp["content"]}
            if resp["tool_calls"]:
                assistant["tool_calls"] = resp["tool_calls"]
            messages.append(assistant)

            if not resp["tool_calls"]:
                break

            for tc in resp["tool_calls"]:
                args = json.loads(tc["arguments"] or "{}")
                exec = ToolExecution(call_id=tc["id"], name=tc["name"], arguments=args, agent=self)
                result = await self.runtime.execute(exec)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": self._tool_content(result),
                    }
                )
                messages.extend(exec.deferred_contexts)

                if exec.concludes_turn:
                    break

        await self.events.serial("agent/session-end", {"messages": messages})
        return AgentResult(messages=messages, final_answer=self._final(messages))

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
