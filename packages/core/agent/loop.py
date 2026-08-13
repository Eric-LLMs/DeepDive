"""Agent main loop: hardcoded while loop + hook extension points.

Modeled after claude-code / openclaw: the main flow is an explicit while loop (small, testable),
with extension injected via hooks (SESSION_START / PRE_TOOL_USE / POST_TOOL_USE / SESSION_END),
rather than splitting the flow into config nodes. Only RAG uses a config-node DAG.
"""
import json
from dataclasses import dataclass
from typing import Protocol

from core.agent.context import ContextBuilder
from core.agent.plugins.hooks import HookContext, HookEvent
from core.agent.plugins.manager import PluginManager
from core.agent.tools import ToolRegistry


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
        registry: ToolRegistry,
        plugins: PluginManager | None = None,
        context: ContextBuilder | None = None,
        max_steps: int = 5,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.plugins = plugins
        self.context = context
        self.max_steps = max_steps

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

        tools = self.registry.get_for_agent()
        await self._dispatch(HookEvent.SESSION_START, HookContext(event=HookEvent.SESSION_START, messages=messages))

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

                blocked, updated_args = await self._dispatch(
                    HookEvent.PRE_TOOL_USE,
                    HookContext(
                        event=HookEvent.PRE_TOOL_USE,
                        tool_name=tc["name"],
                        tool_args=args,
                        messages=messages,
                    ),
                )
                if blocked:
                    result = json.dumps({"error": "blocked by hook"})
                else:
                    if updated_args:
                        args = updated_args
                    result = (await self.registry.call(tc["name"], args)).to_json()

                messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": result}
                )

                await self._dispatch(
                    HookEvent.POST_TOOL_USE,
                    HookContext(
                        event=HookEvent.POST_TOOL_USE,
                        tool_name=tc["name"],
                        tool_args=args,
                        messages=messages,
                    ),
                )

        await self._dispatch(HookEvent.SESSION_END, HookContext(event=HookEvent.SESSION_END, messages=messages))

        return AgentResult(messages=messages, final_answer=self._final(messages))

    async def _dispatch(self, event: HookEvent, ctx: HookContext) -> tuple[bool, dict | None]:
        if not self.plugins:
            return False, None
        blocked, updated_args, new_msgs = await self.plugins.dispatch(event, ctx)
        ctx.messages.extend(new_msgs)
        return blocked, updated_args

    @staticmethod
    def _final(messages: list[dict]) -> str:
        for m in reversed(messages):
            if m["role"] == "assistant" and m.get("content"):
                return m["content"]
        return ""
