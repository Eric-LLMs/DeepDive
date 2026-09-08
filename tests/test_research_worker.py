"""Phase 2A worker-side action-space tests (the auto-run tool whitelist).

The research auto-run must present the model a FIXED, skill-scoped tool set — never the
gateway's mount-as-you-go surface, which is how it was browsing to ``bash`` and burning
LLM calls on hallucinated tools. Pinned here:

1. ``_research_visible_tools`` keeps ``deep_research``'s declared ``allowed_tools`` order
   verbatim with the ``skill`` loader appended LAST (never sorted / reshuffled), is
   exactly the 11-name set (nothing extra, nothing missing), and single-sources every
   schema from ``kernel.runtime.schemas()``.
2. Missing skill or unregistered tool fails the turn FAST (RuntimeError), never silently.
3. The declared set is anchored to the SKILL.md frontmatter (single source of truth), and
   the ``SkillScopeEnforcer`` scope agrees on the boundary (with the honest known
   residual: tool_search stays enforcer-core-exempt — the fix removes it from the
   LLM-facing schemas, not from the execution-time exemption).
4. Cross-step structural statics: the exact same full schema objects reach the model at
   every step (deep dict equality, not name lists).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from agent import (
    Context,
    PluginManager,
    ReactLoopAgent,
    SkillRegistry,
    SystemPrompt,
    ToolOutput,
    ToolRuntime,
    define_tool,
    text_block,
)
from agent.harness import FakeLLM, assistant, tool_call
from agent.skills.registry import SkillScopeEnforcer, skill_tool
from apps.worker.tasks import _research_visible_tools
from plugins.research.plugin import register_research_plugins
from tests._drive_fakes import make_drive

SKILL_PATH = Path("skills/deep_research.skill.md")

# The complete legal tool space of a research auto-run, in declared SKILL.md order with
# the skill loader appended last. Hard-coded on purpose: any drift here is the incident.
EXPECTED_VISIBLE_ORDER = [
    "research_project",
    "research_artifact",
    "research_state",
    "research_evidence",
    "research_gate",
    "research_run",
    "research_scrape",
    "rag_search",
    "web_search",
    "search_social",
    "skill",
]
EXPECTED_VISIBLE_SET = set(EXPECTED_VISIBLE_ORDER)


def _stub_tool(name: str, description: str = "stub"):
    async def body(args, exec):
        return {"ok": True}

    return define_tool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": [],
        },
        output=ToolOutput(schema={"type": "object"}, render=lambda a, v: [text_block("ok")]),
        execute=body,
    )


def _kernel(tmp_path, *, skills_dir: Path = Path("skills"), with_research: bool = True):
    """A fake kernel-shaped object: real research schemas + stub search tools + skill loader."""
    skills = SkillRegistry.from_dir(skills_dir)
    runtime = ToolRuntime()
    if with_research:
        ctx = Context()
        ctx.provide("drive", make_drive(tmp_path))
        ctx.provide("research_scratch", tmp_path / "scratch")
        manager = PluginManager(runtime, SkillRegistry(), ctx)
        register_research_plugins(manager, ctx)
    for name in ("rag_search", "web_search", "search_social"):
        runtime.register(_stub_tool(name))
    runtime.register(skill_tool(skills))
    return SimpleNamespace(skills=skills, runtime=runtime)


class TestVisibleToolWhitelist:
    def test_order_is_declaration_plus_skill_last_never_sorted(self, tmp_path):
        kernel = _kernel(tmp_path)
        names = [t["function"]["name"] for t in _research_visible_tools(kernel)]
        skill = kernel.skills.get("deep_research")
        # Order authority: the skill's declaration, verbatim, with the loader appended LAST.
        assert names == list(skill.allowed_tools) + ["skill"]
        assert names == EXPECTED_VISIBLE_ORDER
        assert names[-1] == "skill"
        # A regression to sorted() would reorder research_project/research_artifact — pin it.
        assert names != sorted(names)

    def test_set_is_exactly_eleven_no_extras_no_gaps(self, tmp_path):
        kernel = _kernel(tmp_path)
        names = {t["function"]["name"] for t in _research_visible_tools(kernel)}
        assert names == EXPECTED_VISIBLE_SET  # strict set-equality, 11 names
        for banned in ("bash", "read_file", "tool_search", "memory_search", "plan"):
            assert banned not in names

    def test_schemas_are_single_sourced_from_the_runtime(self, tmp_path):
        kernel = _kernel(tmp_path)
        visible = _research_visible_tools(kernel)
        found = {s["name"]: s for s in kernel.runtime.schemas()}
        for entry in visible:
            assert set(entry) == {"type", "function"}
            assert entry["type"] == "function"
            # Not a hand-written copy: byte-identical to the registered schema.
            assert entry["function"] == found[entry["function"]["name"]]
        # The research schemas carry the real parameter trees (deep, not stubs).
        ev = next(e["function"] for e in visible if e["function"]["name"] == "research_evidence")
        assert "enum" in ev["parameters"]["properties"]["action"]

    def test_missing_skill_fails_fast(self, tmp_path):
        skills = SkillRegistry()  # deep_research absent
        runtime = ToolRuntime()
        runtime.register(skill_tool(skills))
        kernel = SimpleNamespace(skills=skills, runtime=runtime)
        with pytest.raises(RuntimeError, match="deep_research"):
            _research_visible_tools(kernel)

    def test_unregistered_tool_fails_fast_and_names_it(self, tmp_path):
        kernel = _kernel(tmp_path, with_research=False)
        with pytest.raises(RuntimeError, match="not registered") as exc:
            _research_visible_tools(kernel)
        assert "research_project" in str(exc.value)

    def test_frontmatter_is_the_single_source(self, tmp_path):
        # The registry loader must not reorder the frontmatter, and the whitelist helper
        # must ride on the file — if either side "helpfully" sorts, this diverges.
        raw = SKILL_PATH.read_text(encoding="utf-8")
        line = next(l for l in raw.splitlines() if l.startswith("allowed_tools:"))
        declared = [n.strip() for n in line.split(":", 1)[1].split(",")]
        assert declared == EXPECTED_VISIBLE_ORDER[:-1]  # file order, skill not declared
        skill = SkillRegistry.from_dir(SKILL_PATH.parent).get("deep_research")
        assert skill.allowed_tools == declared


class TestScopeEnforcerBoundary:
    def test_enforcer_scope_matches_the_visible_set(self, tmp_path):
        kernel = _kernel(tmp_path)
        enforcer = SkillScopeEnforcer(kernel.skills)
        allowed = enforcer._allowed(["deep_research"])
        # Every visible tool stays executable while the skill is active...
        assert EXPECTED_VISIBLE_SET <= allowed
        # ...and the drifted extras stay denied at execution time.
        for banned in ("bash", "read_file"):
            assert banned not in allowed

    def test_tool_search_residual_is_recorded_not_claimed_blocked(self, tmp_path):
        # HONEST residual (acceptance wording): tool_search is removed from the LLM-facing
        # schemas (the whitelist never includes it), but the enforcer keeps it core-exempt
        # (registry.py is a red line). A hallucinated tool_search therefore remains
        # *executable* if the model invents the call — it simply has no longer been
        # advertised, and SKILL.md forbids attempting it.
        kernel = _kernel(tmp_path)
        allowed = SkillScopeEnforcer(kernel.skills)._allowed(["deep_research"])
        assert "tool_search" in allowed  # exempt by design — documented, not denied
        visible = {t["function"]["name"] for t in _research_visible_tools(kernel)}
        assert "tool_search" not in visible  # gone from the model-facing action space


class TestCrossStepSchemaStatics:
    async def test_identical_full_schemas_reach_every_step(self, tmp_path):
        # The loop returns the caller-provided tools verbatim at every step, so the model
        # sees byte-stable schemas across the whole turn (prefix-cache friendly, and no
        # mid-run tool appears/disappears to bait drift). Deep compare the FULL schema
        # objects — name/description/parameters/enum trees — not just name lists.
        kernel = _kernel(tmp_path)
        visible = _research_visible_tools(kernel)
        llm = FakeLLM([
            tool_call("c1", "search_social", {"q": "tomato fruit"}),
            assistant("done"),
        ])
        agent = ReactLoopAgent(llm, kernel.runtime, SystemPrompt())
        result = await agent.run("go", tools=visible, max_steps=5)
        assert result.final_answer == "done"
        assert len(llm.calls) == 2
        step1, step2 = (llm.calls[0][1], llm.calls[1][1])
        assert step1 == visible
        assert step2 == visible  # full structural equality, every step
        # Spot-check the deep tree survived untouched (not a name-stub projection).
        ev = next(t["function"] for t in step2 if t["function"]["name"] == "research_evidence")
        assert ev["parameters"]["properties"]["action"]["enum"] == [
            "record_node", "mutate_node", "invalidate_downstream",
            "adjudicate",  # P3-8: the atomic EVIDENCE closure
            "verify", "verify_batch",  # P3-1 additive contract
        ]
        assert "link_edge" not in str(step2)  # hidden actions absent from the whole array

    async def test_bash_is_invisible_and_untouchable_for_the_turn(self, tmp_path):
        # Even though the runtime never registered bash at all here, assert the whitelist
        # shape cannot smuggle it in: the loop only ever advertises the 11 given tools.
        kernel = _kernel(tmp_path)
        visible = _research_visible_tools(kernel)
        llm = FakeLLM([assistant("done")])
        agent = ReactLoopAgent(llm, kernel.runtime, SystemPrompt())
        await agent.run("go", tools=visible, max_steps=3)
        names = [t["function"]["name"] for t in llm.calls[0][1]]
        assert "bash" not in names and "read_file" not in names
