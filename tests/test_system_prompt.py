"""Tests for the SystemPrompt layered assembly + rendering."""
import pytest

from agent.system_prompt import SystemPrompt, render_prompt


async def test_sections_sorted_by_order():
    sp = SystemPrompt()
    sp.section("b", 200, "second")
    sp.section("a", 0, "first")
    sp.section("c", 100, "middle")

    asm = await sp.assemble()
    assert asm.sections == ["first", "middle", "second"]


async def test_variable_interpolation():
    sp = SystemPrompt()
    sp.variable("name", lambda ctx: "Alice")
    sp.section("greeting", 0, "Hello {{name}}!")

    asm = await sp.assemble()
    assert render_prompt(asm) == "Hello Alice!"


async def test_empty_sections_dropped():
    sp = SystemPrompt()
    sp.section("real", 0, "keep me")
    sp.section("empty", 1, "")
    sp.section("none", 2, lambda ctx: None)

    asm = await sp.assemble()
    assert asm.sections == ["keep me"]
    assert render_prompt(asm) == "keep me"


async def test_async_section_resolves():
    sp = SystemPrompt()

    async def dynamic(ctx):
        return f"hello {ctx['user_msg']}"

    sp.section("dyn", 0, dynamic)

    asm = await sp.assemble({"user_msg": "world"})
    assert asm.sections == ["hello world"]


def test_duplicate_section_raises():
    sp = SystemPrompt()
    sp.section("a", 0, "x")
    with pytest.raises(ValueError):
        sp.section("a", 1, "y")
