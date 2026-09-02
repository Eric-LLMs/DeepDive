"""Regression tests: a Research-handoff turn grants its session WRITE + NETWORK.

The Research OS workflow is chat-driven — the desktop "＋ Research" button creates the task
folder via HTTP, then a fresh chat resumes it. The agent must be able to advance the task's
state machine, write scratch artifacts, and run its search tools WITHOUT a human approval
per call. The chat router sinks a ``handoff: {kind: "research", ...}`` into the turn context;
the :class:`~agent.security.sandbox.Sandbox` reads it and raises the turn's grant from
READ-only to READ+WRITE+NETWORK. A normal turn (no research handoff) stays READ-only —
WRITE still ASKs and needs a human approver.
"""
from agent.engine.context import AgentTurn, bind_turn
from agent.security.sandbox import Sandbox
from agent.tools.definition import ToolDefinition, ToolOutput
from agent.tools.tool_permissions import ToolPermission


def _tool(name: str, permission) -> ToolDefinition:
    async def execute(args, exec):  # noqa: ANN202
        return {}

    return ToolDefinition(
        name=name,
        description="",
        parameters={"type": "object", "properties": {}},
        output=ToolOutput(),
        execute=execute,
        permission=permission,
    )


def test_normal_turn_stays_read_only():
    bind_turn(AgentTurn(user_msg="hi"))
    sandbox = Sandbox()
    write = _tool("research_artifact", {ToolPermission.READ, ToolPermission.WRITE})
    network = _tool("web_search", {ToolPermission.NETWORK})
    assert sandbox.check(write, {}).value == "ask"
    assert sandbox.check(network, {}).value == "ask"


def test_research_handoff_turn_grants_write_and_network():
    bind_turn(
        AgentTurn(
            user_msg="hi",
            context={"handoff": {"kind": "research", "project_id": "t1"}},
        )
    )
    sandbox = Sandbox()
    write = _tool("research_artifact", {ToolPermission.READ, ToolPermission.WRITE})
    network = _tool("web_search", {ToolPermission.NETWORK})
    assert sandbox.check(write, {}).value == "allow"
    assert sandbox.check(network, {}).value == "allow"
