"""Smoke tests for the tool runtime."""
from core.agent.decisions import (
    PreToolDecision,
    ToolExecution,
    ToolExecutionFailure,
    ToolExecutionSuccess,
)
from core.agent.runtime import ToolRuntime
from core.agent.tools import ToolOutput, define_tool
from core.agent import text_block


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


async def test_execute_success_and_result_emitted():
    runtime = ToolRuntime()
    runtime.register(_echo_tool())

    emitted = []

    async def on_result(payload):
        emitted.append(payload["result"])

    runtime.events.observe("tools/result", on_result)

    result = await runtime.execute(ToolExecution(call_id="1", name="echo", arguments={"x": 42}))

    assert isinstance(result, ToolExecutionSuccess)
    assert result.value == {"echo": 42}
    assert [b.text for b in result.content] == ["42"]
    assert len(emitted) == 1
    assert emitted[0] is result


async def test_pre_execute_deny_short_circuits():
    runtime = ToolRuntime()

    called = []

    async def body_spy(args, exec):
        called.append(1)
        return {"echo": args["x"]}

    runtime.register(
        define_tool(
            name="spy",
            description="",
            parameters={"type": "object"},
            output=ToolOutput(),
            execute=body_spy,
        )
    )

    async def deny(exec, next_):
        return PreToolDecision.deny("nope")

    runtime.events.on("tools/pre-execute", deny)

    result = await runtime.execute(ToolExecution(call_id="1", name="spy", arguments={}))
    assert isinstance(result, ToolExecutionFailure)
    assert result.error.message == "nope"
    assert called == []


async def test_guard_deny_is_monotonic():
    runtime = ToolRuntime()
    runtime.register(_echo_tool())

    async def guard(exec):
        return "blocked by guard"

    runtime.guard(guard)

    result = await runtime.execute(ToolExecution(call_id="1", name="echo", arguments={"x": 1}))
    assert isinstance(result, ToolExecutionFailure)
    assert result.error.message == "blocked by guard"


async def test_args_validation_failure():
    runtime = ToolRuntime()
    runtime.register(_echo_tool())

    result = await runtime.execute(
        ToolExecution(call_id="1", name="echo", arguments={"x": "not-an-int"})
    )
    assert isinstance(result, ToolExecutionFailure)
    assert result.error.info == {"name": "invalid_args"}
