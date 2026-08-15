"""Tests for the ReactLoopAgent step pipeline (offline, with FakeLLM).

Verifies: a scripted tool-call is dispatched through the runtime, the tool result is
committed back into the message list, session hooks fire, and the persistent session memory
receives messages and is closed on session end.
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


class _FakeMemory:
    def __init__(self):
        self.messages = []
        self.closed = False

    def record_event(self, type_, payload):
        pass

    async def append_message(self, role, text):
        self.messages.append((role, text))

    async def close(self):
        self.closed = True


async def test_tool_call_then_final_answer_and_close():
    runtime = ToolRuntime()
    runtime.register(_echo_tool())
    llm = FakeLLM([tool_call("c1", "echo", {"x": 7}), assistant("all done")])
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())

    started = []
    ended = []
    runtime.events.observe("agent/session-start", lambda p: started.append(p))
    runtime.events.observe("agent/session-end", lambda p: ended.append(p))

    mem = _FakeMemory()
    result = await agent.run("hi", session_memory=mem)

    assert result.final_answer == "all done"
    assert [m["role"] for m in result.messages] == ["user", "assistant", "tool", "assistant"]
    assert result.messages[2]["content"] == "7"

    assert started == [{"user_msg": "hi"}]
    assert len(ended) == 1

    assert mem.closed is True
    assert mem.messages == [("user", "hi"), ("assistant", "all done")]


async def test_plain_answer_no_tool_call():
    runtime = ToolRuntime()
    llm = FakeLLM([assistant("just an answer")])
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())

    result = await agent.run("hello")

    assert result.final_answer == "just an answer"
    assert [m["role"] for m in result.messages] == ["user", "assistant"]
