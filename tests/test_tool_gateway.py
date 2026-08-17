"""Tests for the deferred tool-loading gateway (catalog blurb, mount, allow/deny)."""
import pytest
from agent.tool_gateway import (
    ToolCatalog,
    ToolGateway,
    ToolVisibilityPolicy,
    tool_search_tool,
)
from agent.tools import ToolDefinition, ToolOutput, define_tool


def _def(name: str, description: str, params: dict | None = None) -> ToolDefinition:
    return define_tool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": params or {},
        },
        output=ToolOutput(schema={"type": "string"}, render=lambda a, v: [str(v)]),
        execute=lambda args, exec: name,
    )


@pytest.fixture
def runtime():
    from agent.runtime import ToolRuntime

    rt = ToolRuntime()
    rt.register(_def("rag_search", "Search learning material chunks for a query."))
    rt.register(_def("edit_file", "Edit a file inside the workspace."))
    return rt


async def test_catalog_carries_blurbs_not_full_schemas(runtime):
    catalog = ToolCatalog(runtime)
    entries = catalog.entries()

    assert {e.name for e in entries} == {"rag_search", "edit_file"}
    for entry in entries:
        assert isinstance(entry.blurb, str)
        assert len(entry.blurb) <= 120
        assert "parameters" not in entry.__dict__  # no full schema leaked

    index = catalog.render_index(budget_chars=300)
    assert "- rag_search:" in index
    assert "- edit_file:" in index


async def test_tool_search_mounts_schema_for_next_step(runtime):
    gateway = ToolGateway(runtime)
    before = {t["function"]["name"] for t in gateway.visible_schemas({})}
    assert "edit_file" not in before  # not resident → deferred

    # Simulate the model calling tool_search: it mounts matches for subsequent steps.
    tool = tool_search_tool(gateway.catalog, gateway)
    result = await tool.execute({"query": "edit file"}, None)
    assert [r["name"] for r in result] == ["edit_file"]

    after = {t["function"]["name"] for t in gateway.visible_schemas({})}
    assert "edit_file" in after  # now directly callable
    schema = next(t["function"] for t in gateway.visible_schemas({}) if t["function"]["name"] == "edit_file")
    assert "parameters" in schema  # full schema, not the blurb


async def test_core_tools_always_visible(runtime):
    runtime.register(_def("tool_search", "Search the tool catalog."))
    runtime.register(_def("skill", "Load a skill's instructions."))
    gateway = ToolGateway(runtime, core_names=("tool_search", "skill"))
    names = {t["function"]["name"] for t in gateway.core_schemas()}
    assert {"tool_search", "skill"} <= names


async def test_deny_overrides_allow_and_mount(runtime):
    policy = ToolVisibilityPolicy()
    gateway = ToolGateway(runtime, policy=policy)
    gateway.mount("rag_search")
    policy.allow("rag_search")

    assert gateway.visible_schemas({})  # rag_search visible

    policy.deny("rag_search")
    names = {t["function"]["name"] for t in gateway.visible_schemas({})}
    assert "rag_search" not in names  # deny beats allow + mount


async def test_present_as_scopes_visible_set(runtime):
    policy = ToolVisibilityPolicy()
    gateway = ToolGateway(runtime, policy=policy)
    policy.present_as("readonly", ["rag_search"])

    scoped = {t["function"]["name"] for t in gateway.visible_schemas({"tool_mode": "readonly"})}
    assert scoped == {"rag_search"}
    assert gateway.visible_schemas({}) != scoped  # other modes see the default set
