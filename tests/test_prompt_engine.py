"""Tests for the cache-boundary prompt engine (PromptZone + CacheBoundaryAssembler).

Verifies the three-zone partition, byte-identical stable head across assembles, durable
``inject()`` across steps, and that the internal ``CACHE_BOUNDARY`` separator never leaks
into the rendered prompt.
"""

from agent.system_prompt import (
    CACHE_BOUNDARY,
    CacheBoundaryAssembler,
    PromptZone,
    render_prompt,
)


async def test_zones_partition_into_separate_fields():
    asm = CacheBoundaryAssembler()
    asm.section("soul", 0, "identity", zone=PromptZone.STATIC_PREFIX)
    asm.section("conventions", 10, "project rules", zone=PromptZone.PROJECT_CONTEXT)
    asm.section("memory", 20, "recalled memories", zone=PromptZone.DYNAMIC_SUFFIX)

    assembly = await asm.assemble({"user_msg": "hi"})

    assert assembly.static_prefix == "identity"
    assert assembly.project_context == "project rules"
    assert assembly.dynamic_suffix == "recalled memories"


async def test_static_head_byte_identical_across_assembles():
    asm = CacheBoundaryAssembler()
    asm.section("soul", 0, "You are DeepDive.", zone=PromptZone.STATIC_PREFIX)
    asm.section("conventions", 10, "Use tools.", zone=PromptZone.PROJECT_CONTEXT)

    first = await asm.assemble({})
    key1 = asm.snapshot_key()
    second = await asm.assemble({})
    key2 = asm.snapshot_key()

    assert render_prompt(first) == "You are DeepDive.\n\nUse tools."
    assert render_prompt(first) == render_prompt(second)  # stable head is byte-identical
    assert key1 == key2
    assert len(key1) == 16  # sha256 hex, truncated for the prefix-cache identity


async def test_only_dynamic_changes_per_step():
    asm = CacheBoundaryAssembler()
    asm.section("soul", 0, "identity", zone=PromptZone.STATIC_PREFIX)
    asm.section("memory", 20, lambda ctx: ctx.get("recall", ""), zone=PromptZone.DYNAMIC_SUFFIX)

    first = await asm.assemble({"recall": "alpha"})
    assert first.static_prefix == "identity"  # stable head, independent of the dynamic suffix
    assert render_prompt(first) == "identity\n\nalpha"

    second = await asm.refresh_dynamic({"recall": "beta"})
    third = await asm.refresh_dynamic({"recall": "beta"})

    assert second == "beta"
    assert third == "beta"  # unchanged → loop skips re-sending the system prompt
    assert first.static_prefix == "identity"  # head unchanged; only the suffix would differ


async def test_inject_survives_across_steps_and_clears_on_new_turn():
    from agent.context import AgentTurn, bind_turn

    asm = CacheBoundaryAssembler()
    asm.section("soul", 0, "identity", zone=PromptZone.STATIC_PREFIX)
    asm.section("memory", 20, "", zone=PromptZone.DYNAMIC_SUFFIX)

    turn = AgentTurn(user_msg="hi")
    bind_turn(turn)
    asm.inject("user set target to level 3", name="target")  # delegates to the bound turn
    step1 = await asm.refresh_dynamic({"turn": turn})
    step2 = await asm.refresh_dynamic({"turn": turn})

    assert "level 3" in step1
    assert step1 == step2  # durable across steps

    turn2 = AgentTurn(user_msg="new run")
    bind_turn(turn2)  # new turn → injected content empty
    assert await asm.refresh_dynamic({"turn": turn2}) == ""


async def test_rendered_prompt_contains_no_boundary_marker():
    asm = CacheBoundaryAssembler()
    asm.section("soul", 0, "identity", zone=PromptZone.STATIC_PREFIX)
    asm.section("memory", 20, "dynamic", zone=PromptZone.DYNAMIC_SUFFIX)

    text = render_prompt(await asm.assemble({}))

    # the marker is an internal separator only; the model must never see it
    assert CACHE_BOUNDARY not in text
    assert text.index("identity") < text.index("dynamic")  # stable head precedes the suffix
    assert text == "identity\n\ndynamic"


async def test_legacy_flat_assembly_still_renders():
    """Backward compatibility: a plain SystemPrompt renders flat with no boundary."""
    from agent.system_prompt import SystemPrompt

    sp = SystemPrompt()
    sp.section("a", 0, "one")
    sp.section("b", 1, "two")

    text = render_prompt(await sp.assemble({}))
    assert CACHE_BOUNDARY not in text
    assert text == "one\n\ntwo"
