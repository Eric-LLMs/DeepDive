"""Tests for skill ``allowed_tools`` enforcement (SkillScopeEnforcer).

Covers the hard scoped allowlist semantics: unrestricted when no skill (or no allowlist) is
active, denied outside the union of declared allowlists, core meta-tools always permitted,
and skills recording themselves as active when loaded via the ``skill`` meta-tool.
"""
from agent.engine.context import AgentTurn, bind_turn
from agent.engine.decisions import ToolExecution
from agent.skills.registry import Skill, SkillRegistry, SkillScopeEnforcer


def _registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.register(
        Skill(
            name="fact_check",
            description="d",
            instructions="i",
            keywords=["verify"],
            allowed_tools=["search_social", "web_search"],
        )
    )
    reg.register(Skill(name="loose", description="d", instructions="i"))
    return reg


def _exec(name: str) -> ToolExecution:
    return ToolExecution(call_id="1", name=name, arguments={})


async def test_no_skill_active_is_unrestricted():
    guard = SkillScopeEnforcer(_registry()).guard()
    bind_turn(AgentTurn(user_msg="hi"))

    assert await guard(_exec("rag_search")) is None


async def test_active_skill_denies_out_of_scope_tool():
    turn = AgentTurn(user_msg="hi")
    bind_turn(turn)
    turn.activate_skill("fact_check")
    guard = SkillScopeEnforcer(_registry()).guard()

    reason = await guard(_exec("rag_search"))
    assert reason is not None
    assert "skill scope" in reason
    assert "fact_check" in reason


async def test_allowed_tools_and_core_pass_under_scope():
    turn = AgentTurn(user_msg="hi")
    bind_turn(turn)
    turn.activate_skill("fact_check")
    guard = SkillScopeEnforcer(_registry()).guard()

    assert await guard(_exec("search_social")) is None
    assert await guard(_exec("web_search")) is None
    assert await guard(_exec("skill")) is None
    assert await guard(_exec("tool_search")) is None
    assert await guard(_exec("memory_save")) is None


async def test_skill_without_allowlist_imposes_no_scope():
    turn = AgentTurn(user_msg="hi")
    bind_turn(turn)
    turn.activate_skill("loose")
    guard = SkillScopeEnforcer(_registry()).guard()

    assert await guard(_exec("rag_search")) is None


async def test_union_of_declared_allowlists_scopes_even_with_loose_skill():
    turn = AgentTurn(user_msg="hi")
    bind_turn(turn)
    turn.activate_skill("fact_check")
    turn.activate_skill("loose")
    guard = SkillScopeEnforcer(_registry()).guard()

    assert await guard(_exec("web_search")) is None
    assert await guard(_exec("rag_search")) is not None


async def test_unknown_skill_name_limits_to_core():
    turn = AgentTurn(user_msg="hi")
    bind_turn(turn)
    turn.activate_skill("nope")
    guard = SkillScopeEnforcer(_registry()).guard()

    assert await guard(_exec("rag_search")) is not None
    assert await guard(_exec("skill")) is None
