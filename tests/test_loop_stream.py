"""Tests for the streaming agent path (ReactLoopAgent.run_stream).

Same offline setup as test_loop.py (scripted FakeLLM + ToolRuntime), but exercised through
the streaming variant: the loop must forward thinking/content deltas as separate events,
dispatch tool calls exactly like ``run``, and close the persistent session memory on end.
"""
from agent import (
    ReactLoopAgent,
    SystemPrompt,
    ToolOutput,
    ToolRuntime,
    define_tool,
    text_block,
)
from agent.harness import FakeLLM, stream_assistant, tool_call


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


async def _collect(agent, user_msg, memory):
    events = []
    async for evt in agent.run_stream(user_msg, session_memory=memory):
        events.append(evt)
    return events


async def test_stream_forward_thinking_and_content_separately():
    runtime = ToolRuntime()
    llm = FakeLLM([stream_assistant(thinking="need to recall attention", content="注意力机制是一种…")])
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())
    memory = _FakeMemory()

    events = await _collect(agent, "什么是注意力机制?", memory)

    thinking = [e for e in events if e["type"] == "thinking"]
    content = [e for e in events if e["type"] == "content"]
    done = [e for e in events if e["type"] == "done"]

    # Reasoning and answer are forwarded as separate events, in order.
    assert [e["data"] for e in thinking] == ["need to recall attention"]
    assert [e["data"] for e in content] == ["注意力机制是一种…"]
    assert done, "a done event must close the turn"
    assert done[-1]["data"]["answer"] == "注意力机制是一种…"
    assert done[-1]["data"]["usage"]["completion_tokens"] == 1
    # The user message + final answer are persisted; memory is closed on end.
    assert memory.messages == [("user", "什么是注意力机制?"), ("assistant", "注意力机制是一种…")]
    assert memory.closed


async def test_stream_tool_call_then_final_answer():
    runtime = ToolRuntime()
    runtime.register(_echo_tool())
    llm = FakeLLM(
        [
            tool_call("c1", "echo", {"x": 7}),
            stream_assistant(thinking="summarize", content="all done"),
        ]
    )
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())
    memory = _FakeMemory()

    events = await _collect(agent, "run it", memory)

    content = [e for e in events if e["type"] == "content"]
    tools = [e for e in events if e["type"] == "tool"]
    done = [e for e in events if e["type"] == "done"]
    assert [e["data"] for e in content] == ["all done"]
    # A tool event announces each dispatched tool by name before it runs.
    assert [e["data"]["name"] for e in tools] == ["echo"]
    assert done[-1]["data"]["answer"] == "all done"
    # Two streamed LLM calls (tool round + final), usage accumulated across both.
    assert len(llm.calls) == 2
    assert done[-1]["data"]["usage"]["completion_tokens"] == 2
    assert memory.messages[-1] == ("assistant", "all done")
    assert memory.closed


async def test_stream_done_messages_include_tool_round():
    """The done event's message list must reflect the tool round (echo → result → answer)."""
    runtime = ToolRuntime()
    runtime.register(_echo_tool())
    llm = FakeLLM(
        [
            tool_call("c1", "echo", {"x": 7}),
            stream_assistant(content="7!"),
        ]
    )
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())

    done = None
    async for evt in agent.run_stream("run it"):
        if evt["type"] == "done":
            done = evt
    roles = [m["role"] for m in done["data"]["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert done["data"]["messages"][2]["content"] == "7"
