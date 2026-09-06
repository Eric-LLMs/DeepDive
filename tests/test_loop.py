"""Tests for the ReactLoopAgent step pipeline (offline, with FakeLLM).

Verifies: a scripted tool-call is dispatched through the runtime, the tool result is
committed back into the message list, session hooks fire, and the persistent session memory
receives messages and is closed on session end.
"""
from pathlib import Path

from agent import (
    ReactLoopAgent,
    SystemPrompt,
    ToolOutput,
    ToolRuntime,
    define_tool,
    text_block,
)
from agent.engine.context import AgentTurn, current_turn
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


async def test_non_streaming_run_records_llm_duration_in_span():
    """Non-streaming steps must feed the turn span: llm_calls + LLM duration per turn.

    Regression guard: previously the non-streaming ``run`` recorded steps/tools but never
    ``record_llm``, so every non-streaming turn reported ``llm_calls == 0`` (the gap the
    streaming path already covered).
    """
    from agent.engine.context import AgentTurn

    runtime = ToolRuntime()
    runtime.register(_echo_tool())
    llm = FakeLLM([tool_call("c1", "echo", {"x": 7}), assistant("all done")])
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())
    turn = AgentTurn(user_msg="hi")

    result = await agent.run("hi", turn=turn)

    assert result.final_answer == "all done"
    span = turn.span
    d = span.to_dict()
    assert d["llm_calls"] == 2  # one model call per step (tool step + final answer)
    assert d["steps"] == 2
    # record_llm anchors onto the current step (recorded right after record_step).
    assert len(span.steps) == 2
    assert all("llm_duration_ms" in s for s in span.steps)
    # Per-turn totals exposed by the span/audit payload.
    assert d["llm_duration_ms"] >= 0
    assert len(d["tools"]) == 1
    assert d["tool_duration_ms"] > 0
    assert d["duration_s"] >= 0


# ── generic cooperative turn stop (request_stop) ─────────────────────────────
# The stop contract lives on AgentTurn; the loop must honour it WITHOUT knowing
# anything about Research (no stage/gate vocabulary in the loop). These tests use a
# plain non-Research tool to prove the mechanism is generic.

def _stopper_tool(reason: str = "unit-test-reason", sink: list | None = None):
    async def body(args, exec):
        turn = current_turn()
        assert turn is not None, "loop must bind the turn before dispatching tools"
        turn.request_stop(reason)
        if sink is not None:
            sink.append(reason)
        return {"stopped": True}

    return define_tool(
        name="stopper",
        description="requests the generic turn stop",
        parameters={"type": "object", "properties": {}},
        output=ToolOutput(
            schema={"type": "object", "properties": {"stopped": {"type": "boolean"}}},
            render=lambda args, value: [text_block("stop requested")],
        ),
        execute=body,
    )


async def test_request_stop_ends_turn_at_step_boundary():
    """A tool calling ``turn.request_stop`` ends the turn AFTER its step committed.

    The scripted second LLM answer must never be consumed (exactly one LLM call),
    the tool result is still in the message list (in-flight work is not dropped),
    and the turn ends with no error.
    """
    runtime = ToolRuntime()
    runtime.register(_stopper_tool())
    llm = FakeLLM([tool_call("c1", "stopper", {}), assistant("must-not-be-reached")])
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())
    turn = AgentTurn(user_msg="hi")

    result = await agent.run("hi", turn=turn)

    assert len(llm.calls) == 1  # loop broke at the step boundary, no second call
    assert turn.stop_requested is True
    assert turn.stop_reason == "unit-test-reason"
    assert result.error is None
    # Tool result committed before the stop took effect.
    roles = [m["role"] for m in result.messages]
    assert roles == ["user", "assistant", "tool"]
    assert result.messages[2]["content"] == "stop requested"


async def test_request_stop_is_idempotent_first_reason_wins():
    turn = AgentTurn(user_msg="hi")
    assert turn.stop_requested is False and turn.stop_reason is None
    turn.request_stop("first")
    turn.request_stop("second")
    assert turn.stop_requested is True
    assert turn.stop_reason == "first"


async def test_stop_flag_does_not_leak_across_turns():
    """``stop_requested`` is per-turn state: a fresh AgentTurn starts un-stopped."""
    runtime = ToolRuntime()
    runtime.register(_stopper_tool())
    llm = FakeLLM(
        [tool_call("c1", "stopper", {}), assistant("a1"), assistant("a2")]
    )
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())

    turn1 = AgentTurn(user_msg="hi")
    r1 = await agent.run("hi", turn=turn1)
    assert turn1.stop_requested is True and len(llm.calls) == 1

    turn2 = AgentTurn(user_msg="again")
    assert turn2.stop_requested is False  # no residue from the previous turn
    r2 = await agent.run("again", turn=turn2)
    assert r2.final_answer == "a1"
    assert turn2.stop_requested is False


async def test_no_stop_behaves_exactly_as_before():
    """Control: without request_stop the loop runs the full scripted chain."""
    runtime = ToolRuntime()
    runtime.register(_echo_tool())
    llm = FakeLLM([tool_call("c1", "echo", {"x": 1}), assistant("done")])
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())
    turn = AgentTurn(user_msg="hi")

    result = await agent.run("hi", turn=turn)

    assert len(llm.calls) == 2
    assert result.final_answer == "done"
    assert turn.stop_requested is False


def test_loop_stays_decoupled_from_research():
    """Static guard: the generic loop must never learn Research concepts.

    The step-boundary stop must be triggered only through the opaque
    ``AgentTurn.request_stop`` contract — loop.py may not import plugins/research
    or mention stage/gate vocabulary.
    """
    src = (Path(__file__).resolve().parents[1] / "packages" / "agent" / "engine" / "loop.py").read_text(encoding="utf-8")
    assert "plugins" not in src
    for forbidden in ("stage_advanced", "transition_stage", "research_", "GATE", "stage"):
        assert forbidden not in src, f"loop.py leaked a Research concept: {forbidden!r}"
