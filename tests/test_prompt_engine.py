"""Tests for the cache-boundary prompt engine (PromptZone + CacheBoundaryAssembler).

Verifies the three-zone partition, byte-identical stable head across assembles, durable
``inject()`` across steps, and the fixed cache-boundary marker position.
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

    head1 = render_prompt(first).split(CACHE_BOUNDARY)[0]
    head2 = render_prompt(second).split(CACHE_BOUNDARY)[0]
    assert head1 == head2
    assert key1 == key2
    assert len(key1) == 16  # sha256 hex, truncated for the prefix-cache identity


async def test_only_dynamic_changes_per_step():
    asm = CacheBoundaryAssembler()
    asm.section("soul", 0, "identity", zone=PromptZone.STATIC_PREFIX)
    asm.section("memory", 20, lambda ctx: ctx.get("recall", ""), zone=PromptZone.DYNAMIC_SUFFIX)

    first = await asm.assemble({"recall": "alpha"})
    stable_head = render_prompt(first).split(CACHE_BOUNDARY)[0]

    second = await asm.refresh_dynamic({"recall": "beta"})
    third = await asm.refresh_dynamic({"recall": "beta"})

    assert second == "beta"
    assert third == "beta"  # unchanged → loop skips re-sending the system prompt
    assert render_prompt(first).split(CACHE_BOUNDARY)[0] == stable_head


async def test_inject_survives_across_steps_and_clears_on_new_session():
    asm = CacheBoundaryAssembler()
    asm.section("soul", 0, "identity", zone=PromptZone.STATIC_PREFIX)
    asm.section("memory", 20, "", zone=PromptZone.DYNAMIC_SUFFIX)

    asm.begin_session()
    asm.inject("user set target to level 3", name="target")
    step1 = await asm.refresh_dynamic({})
    step2 = await asm.refresh_dynamic({})

    assert "level 3" in step1
    assert step1 == step2  # durable across steps

    asm.begin_session()  # new run → injected content reset
    assert await asm.refresh_dynamic({}) == ""


async def test_boundary_marker_sits_once_between_stable_and_dynamic():
    asm = CacheBoundaryAssembler()
    asm.section("soul", 0, "identity", zone=PromptZone.STATIC_PREFIX)
    asm.section("memory", 20, "dynamic", zone=PromptZone.DYNAMIC_SUFFIX)

    text = render_prompt(await asm.assemble({}))

    assert text.count(CACHE_BOUNDARY) == 1
    assert text.index(CACHE_BOUNDARY) > text.index("identity")
    assert text.index(CACHE_BOUNDARY) < text.index("dynamic")


async def test_legacy_flat_assembly_still_renders():
    """Backward compatibility: a plain SystemPrompt renders flat with no boundary."""
    from agent.system_prompt import SystemPrompt

    sp = SystemPrompt()
    sp.section("a", 0, "one")
    sp.section("b", 1, "two")

    text = render_prompt(await sp.assemble({}))
    assert CACHE_BOUNDARY not in text
    assert text == "one\n\ntwo"
