"""Plan mode: the ``plan`` meta-tool streams a plan event to the turn's progress sink."""
from agent.context import AgentTurn, bind_turn
from agent.decisions import ToolExecution
from agent.plan_tool import plan_tool
from agent.runtime import ToolRuntime


async def test_plan_emits_event_to_turn_progress_sink():
    runtime = ToolRuntime()
    runtime.register(plan_tool())
    emitted = []
    turn = AgentTurn(user_msg="plan this", progress_sink=emitted.append)
    bind_turn(turn)

    result = await runtime.execute(
        ToolExecution(
            call_id="c1",
            name="plan",
            arguments={"goal": "learn fastapi", "steps": ["read docs", "build sample"]},
        )
    )

    assert result.is_error is False
    assert emitted == [
        {"type": "plan", "data": {"goal": "learn fastapi", "steps": ["read docs", "build sample"]}}
    ]
    assert result.value["ok"] is True
    assert result.value["steps"] == ["read docs", "build sample"]


async def test_plan_works_without_a_bound_turn():
    runtime = ToolRuntime()
    runtime.register(plan_tool())

    result = await runtime.execute(
        ToolExecution(call_id="c1", name="plan", arguments={"goal": "g"})
    )

    assert result.is_error is False
    assert result.value["ok"] is True
