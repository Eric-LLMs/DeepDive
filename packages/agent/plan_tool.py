"""Plan mode: a kernel meta-tool that proposes a multi-step plan to the user.

The model calls ``plan`` to present an explicit step breakdown before acting. The tool emits a
``{"type": "plan", "data": {goal, steps}}`` event to the turn's progress sink (streamed to the
client as an SSE frame), and returns a confirmation the model sees as its own result. The loop
needs no change — ``plan`` is just another tool the model may choose.
"""
from __future__ import annotations

import json

from agent.context import current_turn
from agent.decisions import text_block
from agent.tools import ToolDefinition, ToolOutput, define_tool


def plan_tool() -> ToolDefinition:
    async def execute(args: dict, exec) -> dict:
        steps = args.get("steps") or []
        turn = current_turn()
        if turn is not None:
            turn.emit(
                {"type": "plan", "data": {"goal": args.get("goal", ""), "steps": steps}}
            )
        return {
            "ok": True,
            "message": "Plan recorded and presented to the user.",
            "steps": steps,
        }

    return define_tool(
        name="plan",
        description=(
            "Propose a multi-step plan for the user's goal before acting. Provide a short goal "
            "and an ordered list of steps. The plan is shown to the user immediately; use it "
            "when a task benefits from being broken into explicit stages."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The objective the plan addresses."},
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered steps to reach the goal.",
                },
            },
            "required": ["goal"],
        },
        output=ToolOutput(
            schema={"type": "object"},
            render=lambda args, value: [text_block(json.dumps(value, ensure_ascii=False))],
        ),
        execute=execute,
        is_concurrency_safe=True,
    )
