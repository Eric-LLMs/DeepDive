"""Subagents: depth cap, schema filtering, and a full parent→child turn round-trip."""
from agent import (
    ReactLoopAgent,
    SystemPrompt,
    ToolOutput,
    ToolRuntime,
    define_tool,
    text_block,
)
from agent.harness import FakeLLM, assistant, tool_call
from agent.tools.plan_tool import plan_tool
from agent.tools.subagent import _DEPTH_CTX, _filter_schemas, run_subagent_tool
from core.config import settings


def _echo_tool():
    async def body(args, exec):
        return {"echo": args["x"]}

    return define_tool(
        name="echo",
        description="echo a number",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
        output=ToolOutput(
            schema={"type": "object", "properties": {"echo": {"type": "integer"}}},
            render=lambda args, value: [text_block(str(value["echo"]))],
        ),
        execute=body,
    )


def test_schema_filter_excludes_meta_tools():
    runtime = ToolRuntime()
    runtime.register(_echo_tool())
    runtime.register(run_subagent_tool())
    runtime.register(plan_tool())

    names = {s["function"]["name"] for s in _filter_schemas(runtime)}
    assert names == {"echo"}


async def test_depth_cap_returns_error():
    from agent.engine.decisions import ToolExecution

    runtime = ToolRuntime()
    runtime.register(run_subagent_tool())
    _DEPTH_CTX.set(settings.max_subagent_depth)
    try:
        # Depth is checked before the parent-loop requirement, so no turn is needed here.
        result = await runtime.execute(
            ToolExecution(call_id="c1", name="run_subagent", arguments={"prompt": "deep"})
        )
        assert result.value["ok"] is False
        assert "depth" in result.value["error"]
    finally:
        _DEPTH_CTX.set(0)


async def test_parent_delegates_to_child_and_returns_child_answer():
    runtime = ToolRuntime()
    runtime.register(_echo_tool())
    runtime.register(run_subagent_tool())
    # Parent script: step 1 calls run_subagent, child consumes the next response, then the
    # parent answers. Shared FakeLLM script — the child pops the middle response.
    llm = FakeLLM(
        [
            tool_call("c1", "run_subagent", {"prompt": "summarize the goal"}),
            assistant("child result"),
            assistant("parent final"),
        ]
    )
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())

    result = await agent.run("break this down")

    assert result.final_answer == "parent final"
    # The committed tool message carries the child's answer.
    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert any("child result" in m["content"] for m in tool_msgs)


async def test_subagent_requires_a_parent_loop():
    from agent.engine.decisions import ToolExecution

    runtime = ToolRuntime()
    runtime.register(run_subagent_tool())
    # No agent turn: exec.agent is None → the tool refuses.
    result = await runtime.execute(
        ToolExecution(call_id="c1", name="run_subagent", arguments={"prompt": "x"})
    )
    assert result.value["ok"] is False
    assert "only available inside an agent turn" in result.value["error"]
