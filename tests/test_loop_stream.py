"""Tests for the streaming agent path (ReactLoopAgent.run_stream).

Same offline setup as test_loop.py (scripted FakeLLM + ToolRuntime), but exercised through
the streaming variant: the loop must forward thinking/content deltas as separate events,
dispatch tool calls exactly like ``run``, and close the persistent session memory on end.
"""
import asyncio

from agent import (
    ReactLoopAgent,
    SystemPrompt,
    ToolOutput,
    ToolRuntime,
    define_tool,
    text_block,
)
from agent.harness import FakeLLM, stream_assistant, tool_call
from agent.llm.llm_guard import ReliableLLM


def _per_input_llm(per_input: dict[str, dict]) -> FakeLLM:
    """A FakeLLM whose streamed reply is selected by the user message text.

    Unlike the shared-script FakeLLM (``pop(0)``), the output is a pure function of the
    input, so two concurrent turns deterministically get their own reply regardless of
    interleaving.
    """

    class _PerInputLLM(FakeLLM):
        async def chat_stream(self, messages, tools=None, **kw):
            self.calls.append((list(messages), tools))
            user_text = next(
                m["content"] for m in reversed(messages) if m["role"] == "user"
            )
            resp = per_input[user_text]
            if resp.get("thinking"):
                yield {"type": "thinking", "data": resp["thinking"]}
            if resp.get("content"):
                yield {"type": "content", "data": resp["content"]}
            yield {"type": "tool_calls", "data": []}
            yield {"type": "usage", "data": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    return _PerInputLLM([])


def _slow_per_input_llm(per_input: dict[str, list[str]]) -> FakeLLM:
    """A per-input FakeLLM that yields control between every event (network-like).

    Real provider streams suspend on I/O, so two overlapping streams interleave *inside*
    the first-token window — the exact shape that exposed the old ``ReliableLLM._gen``
    instance slot. ``await asyncio.sleep(0)`` reproduces that suspension without a network.
    """

    class _SlowPerInputLLM(FakeLLM):
        async def chat_stream(self, messages, tools=None, **kw):
            self.calls.append((list(messages), tools))
            user_text = next(
                m["content"] for m in reversed(messages) if m["role"] == "user"
            )
            resp = per_input[user_text]
            await asyncio.sleep(0)  # latency before the first token
            for piece in resp:
                yield {"type": "content", "data": piece}
                await asyncio.sleep(0)
            yield {"type": "tool_calls", "data": []}
            await asyncio.sleep(0)
            yield {"type": "usage", "data": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    return _SlowPerInputLLM([])


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


async def test_two_concurrent_turns_complete_independently():
    """Smoke: two overlapping ``run_stream`` turns on one agent each finish with their answer."""
    llm = _per_input_llm(
        {
            "hello A": {"thinking": "think-A", "content": "answer-A"},
            "hello B": {"thinking": "think-B", "content": "answer-B"},
        }
    )
    agent = ReactLoopAgent(ReliableLLM(llm), ToolRuntime(), SystemPrompt())

    async def collect(msg: str):
        events = []
        async for evt in agent.run_stream(msg):
            events.append(evt)
        return msg, events

    results = await asyncio.gather(collect("hello A"), collect("hello B"))

    for msg, think, ans in [("hello A", "think-A", "answer-A"), ("hello B", "think-B", "answer-B")]:
        events = [e for m, e in results if m == msg][0]
        assert [e["data"] for e in events if e["type"] == "thinking"] == [think]
        assert [e["data"] for e in events if e["type"] == "content"] == [ans]
        done = [e for e in events if e["type"] == "done"]
        assert done and done[-1]["data"]["answer"] == ans
    assert len(llm.calls) == 2  # both turns actually reached the LLM


async def test_reliable_llm_concurrent_streams_do_not_cross_talk():
    """Regression (A1): ReliableLLM's stream generator must never live on the instance.

    The old code stored the generator in ``self._gen`` inside ``open_stream`` and read it
    back after ``yield first`` — a real suspension (the inner stream's first-token wait and
    the consumer's await between events) lets the other stream overwrite ``self._gen`` in
    that window, so both consumers then drain the last-writer's generator. With the
    closure-local fix each consumer owns its own generator, so A only ever sees A's deltas.

    The slow fake forces the suspension that a live HTTP stream would produce; each consumer
    also awaits between events, mirroring the loop's persist/telemetry awaits.
    """
    llm = _slow_per_input_llm(
        {"hello A": ["a1", "a2"], "hello B": ["b1", "b2"]}
    )
    reliable = ReliableLLM(llm, timeout_s=5.0)

    async def drain(msg: str):
        content = []
        async for evt in reliable.chat_stream([{"role": "user", "content": msg}]):
            await asyncio.sleep(0)  # consumer await between events (like the loop's persist)
            if evt["type"] == "content":
                content.append(evt["data"])
        return content

    a, b = await asyncio.gather(drain("hello A"), drain("hello B"))
    assert a == ["a1", "a2"]  # never any of B's deltas
    assert b == ["b1", "b2"]  # never any of A's deltas
