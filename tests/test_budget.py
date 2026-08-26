"""Hard per-turn budget guard: the loop aborts once accumulated cost hits the cap.

Also verifies per-step tool progress events (``tool-start`` / ``tool-result``) stream to the
turn's ``progress_sink`` — the other half of the per-step cost/observability work.
"""
from agent import ReactLoopAgent, SystemPrompt, ToolOutput, ToolRuntime, define_tool, text_block
from agent.engine.context import AgentTurn
from agent.harness import FakeLLM, assistant, tool_call

# 1000 prompt + 1000 completion tokens on deepdive-chat costs 0.00075 USD.
_STEP = {
    **tool_call("c1", "echo", {"x": 1}),
    "usage": {"prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000},
}
# The smallest nonzero rounded cost is 1e-6, so any budget below that is immediately exhausted.
_TINY_BUDGET = 1e-9


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


def _agent() -> ReactLoopAgent:
    runtime = ToolRuntime()
    runtime.register(_echo_tool())
    return ReactLoopAgent(FakeLLM([dict(_STEP), assistant("final")]), runtime, SystemPrompt())


async def test_hard_budget_aborts_non_streaming_loop():
    # Control: without a cap the same script runs both steps through to "final".
    control = _agent()
    result = await control.run("go", model="deepdive-chat")
    assert result.final_answer == "final"
    assert len(control.llm.calls) == 2

    # Capped: the budget is exhausted after the first (tool) step, so "final" never runs.
    capped = _agent()
    turn = AgentTurn(user_msg="go", model="deepdive-chat", max_budget_usd=_TINY_BUDGET)
    result = await capped.run("go", turn=turn)

    assert result.final_answer == ""        # never reached the final step
    assert len(capped.llm.calls) == 1       # one LLM call — the loop aborted
    assert result.cost_usd >= _TINY_BUDGET  # the span still reports the exhausted cost


async def test_streaming_budget_yields_error_event():
    agent = _agent()
    turn = AgentTurn(user_msg="go", model="deepdive-chat", max_budget_usd=_TINY_BUDGET)

    events = [evt async for evt in agent.run_stream("go", turn=turn)]

    error = next(e for e in events if e["type"] == "error")
    assert error["data"]["message"] == "per-turn budget exceeded"
    # The tool step still dispatched before the guard tripped.
    assert any(e["type"] == "tool" for e in events)


async def test_tool_progress_events_stream_to_progress_sink():
    runtime = ToolRuntime()
    runtime.register(_echo_tool())
    llm = FakeLLM([dict(_STEP), assistant("done")])
    agent = ReactLoopAgent(llm, runtime, SystemPrompt())

    progress = []
    result = await agent.run("go", model="deepdive-chat", progress_sink=progress.append)

    assert result.final_answer == "done"
    tools = [e["data"]["name"] for e in progress if e["type"] in ("tool-start", "tool-result")]
    assert tools == ["echo", "echo"]
