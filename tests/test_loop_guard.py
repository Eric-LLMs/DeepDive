"""Tests for ToolLoopTracker and its wiring into the loop (oscillation breaker).

Verifies the unit semantics (identical-call streak, order-insensitive arg hash, identical
error streak, reset) and a loop-level test that the breaker injects forced guidance instead
of letting the turn burn tokens on the same call.
"""
from agent import (
    ReactLoopAgent,
    SystemPrompt,
    ToolOutput,
    ToolRuntime,
    define_tool,
    text_block,
)
from agent.harness import FakeLLM, assistant, tool_call
from agent.loop_guard import ToolLoopTracker


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


async def test_single_call_does_not_break():
    t = ToolLoopTracker()
    t.record("bash", {"cmd": "ls"})
    assert not t.should_break()


async def test_three_identical_calls_break():
    t = ToolLoopTracker()
    for _ in range(3):
        t.record("bash", {"cmd": "ls"})
    assert t.should_break()


async def test_arg_hash_is_order_insensitive():
    t = ToolLoopTracker()
    for args in ({"path": "a", "name": "b"}, {"name": "b", "path": "a"}, {"name": "b", "path": "a"}):
        t.record("fs", args)
    assert t.should_break()  # 3 identical calls, modulo key order


async def test_different_args_reset_the_streak():
    t = ToolLoopTracker()
    t.record("bash", {"cmd": "ls"})
    t.record("bash", {"cmd": "pwd"})
    assert not t.should_break()


async def test_three_identical_errors_break():
    t = ToolLoopTracker()
    for _ in range(3):
        t.record_error("bash", "command not found")
    assert t.should_break()


async def test_reset_clears_streaks():
    t = ToolLoopTracker()
    for _ in range(3):
        t.record("bash", {"cmd": "ls"})
    assert t.should_break()
    t.reset()
    assert not t.should_break()


async def test_loop_injects_guidance_after_repeated_identical_tool_call():
    runtime = ToolRuntime()
    runtime.register(_echo_tool())
    # The model keeps emitting the same tool call; the breaker must not let it loop forever.
    llm = FakeLLM([tool_call("c1", "echo", {"x": 7})] * 3 + [assistant("done")])
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())

    result = await agent.run("hi")

    assert result.final_answer == "done"
    guidance = [m for m in result.messages if (m.get("content") or "").startswith("[system]")]
    assert len(guidance) == 1  # one forced-guidance injection, not a runaway loop
