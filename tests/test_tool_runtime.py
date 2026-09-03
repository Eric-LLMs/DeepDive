"""Smoke tests for the tool runtime."""
from agent import text_block
from agent.engine.decisions import (
    PreToolDecision,
    ToolExecution,
    ToolExecutionFailure,
    ToolExecutionSuccess,
)
from agent.engine.runtime import ToolRuntime
from agent.tools.definition import ToolOutput, define_tool


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


def _node_tool():
    async def body(args, exec):
        return {"id": args["node"]["id"]}

    return define_tool(
        name="record_node",
        description="record a graph node",
        parameters={
            "type": "object",
            "properties": {
                "node": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "label": {"type": "string"}},
                    "required": ["id"],
                }
            },
            "required": ["node"],
        },
        output=ToolOutput(
            schema={"type": "object", "properties": {"id": {"type": "string"}}},
            render=lambda args, value: [text_block(value["id"])],
        ),
        execute=body,
    )


async def test_stringified_object_param_is_decoded_before_validation():
    """An object-typed param double-encoded into a JSON string still reaches the body."""
    runtime = ToolRuntime()
    runtime.register(_node_tool())

    result = await runtime.execute(
        ToolExecution(
            call_id="1",
            name="record_node",
            arguments={"node": '{"id": "n1", "label": "root"}'},
        )
    )
    assert isinstance(result, ToolExecutionSuccess)
    assert result.value == {"id": "n1"}


async def test_object_param_that_is_not_json_still_rejected():
    """A string that cannot parse is not masked; jsonschema reports it as invalid_args."""
    runtime = ToolRuntime()
    runtime.register(_node_tool())

    result = await runtime.execute(
        ToolExecution(call_id="1", name="record_node", arguments={"node": "not-json-at-all"})
    )
    assert isinstance(result, ToolExecutionFailure)
    assert result.error.info == {"name": "invalid_args"}
