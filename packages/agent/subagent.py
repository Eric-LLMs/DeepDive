"""Subagents: a ``run_subagent`` tool that spawns a bounded, schema-filtered child turn.

A subagent is a fresh :class:`AgentTurn` (empty history) run on the same runtime but with a
filtered tool schema — no recursive ``run_subagent`` and no parent-turn meta-tools — capped at
``SUBAGENT_MAX_STEPS`` steps and at ``settings.max_subagent_depth`` nesting. The child's final
answer is returned to the parent as the tool result; the child's messages are discarded. Depth
is tracked via a task-local ``ContextVar`` and the child runs in its own task (a copied
context), so nested subagents on the shared kernel never interfere with the parent turn or with
each other.
"""
from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar

from core.config import settings

from agent.context import AgentTurn, current_turn
from agent.decisions import text_block
from agent.loop import ReactLoopAgent
from agent.tools import ToolDefinition, ToolOutput, define_tool

_DEPTH_CTX: ContextVar[int] = ContextVar("subagent_depth", default=0)

# Parent-turn meta-tools a subagent must not call (they reason about / mutate the parent turn).
_SUBAGENT_EXCLUDED = {"run_subagent", "tool_search", "plan", "revert_to_checkpoint"}

SUBAGENT_MAX_STEPS = 3


def _filter_schemas(runtime) -> list[dict]:
    """Full schemas of every tool a subagent may call (parent meta-tools excluded)."""
    return [
        {"type": "function", "function": s}
        for s in runtime.schemas()
        if s.get("name") not in _SUBAGENT_EXCLUDED
    ]


def run_subagent_tool() -> ToolDefinition:
    async def execute(args: dict, exec) -> dict:
        depth = _DEPTH_CTX.get()
        if depth >= settings.max_subagent_depth:
            return {
                "ok": False,
                "error": f"max subagent depth ({settings.max_subagent_depth}) exceeded",
            }
        parent_loop = getattr(exec, "agent", None)
        if not isinstance(parent_loop, ReactLoopAgent):
            return {"ok": False, "error": "run_subagent is only available inside an agent turn"}
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        parent = current_turn()
        model = args.get("model") or (parent.model if parent is not None else None)
        tools = _filter_schemas(parent_loop.runtime)

        async def _run_child() -> str:
            # Runs in a copied context (asyncio.create_task), so this set never leaks upward.
            _DEPTH_CTX.set(depth + 1)
            child_turn = AgentTurn(
                user_msg=prompt,
                history=[],
                model=model,
                base_url=parent.base_url if parent is not None else None,
                api_key=parent.api_key if parent is not None else None,
                max_budget_usd=settings.max_budget_per_turn_usd,
            )
            # A fresh loop sharing llm/runtime/system_prompt but no gateway: the filtered full
            # schemas are passed directly, and the parent's deferred-loading state is untouched.
            child_loop = ReactLoopAgent(
                llm=parent_loop.llm,
                runtime=parent_loop.runtime,
                system_prompt=parent_loop.system_prompt,
                session_log=parent_loop.session_log,
                max_steps=SUBAGENT_MAX_STEPS,
                gateway=None,
            )
            result = await child_loop.run(
                prompt, turn=child_turn, tools=tools, max_steps=SUBAGENT_MAX_STEPS
            )
            return result.final_answer or "(no answer)"

        task = asyncio.create_task(_run_child())
        try:
            return {"ok": True, "answer": await task}
        finally:
            if not task.done():
                task.cancel()  # parent cancelled mid-subagent → unwind the child

    return define_tool(
        name="run_subagent",
        description=(
            "Delegate a focused subtask to a sub-agent. Provide a self-contained prompt. The "
            "sub-agent runs briefly with a reduced toolset and returns its final answer. Use "
            "this to parallelize independent sub-tasks of a larger goal."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Self-contained instructions for the sub-agent.",
                },
                "model": {"type": "string", "description": "Optional model override."},
            },
            "required": ["prompt"],
        },
        output=ToolOutput(
            schema={"type": "object"},
            render=lambda args, value: [text_block(json.dumps(value, ensure_ascii=False))],
        ),
        execute=execute,
    )
