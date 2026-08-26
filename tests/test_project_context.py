"""Tests for the project-context loader and its wiring into the kernel.

Covers: reading the first existing convention file (``DEEPDIVE.md`` by default, or an explicit
``files`` list), the character cap with a truncation marker, the empty-workspace fallback, and
that a non-empty ``project_context`` renders into the system prompt and feeds ``snapshot_key``
while an absent one renders nothing.
"""
from agent import FakeLLM, ToolRuntime, assistant
from agent.engine.kernel import AgentKernel
from agent.tools.project_context import read_project_context
from agent.prompt.system_prompt import render_prompt


def test_reads_deepdive_md(tmp_path):
    (tmp_path / "DEEPDIVE.md").write_text("project rules here", encoding="utf-8")

    assert read_project_context(tmp_path) == "project rules here"


def test_prefers_first_existing_file_when_files_given(tmp_path):
    (tmp_path / "A.md").write_text("first rules", encoding="utf-8")
    (tmp_path / "B.md").write_text("second rules", encoding="utf-8")

    assert read_project_context(tmp_path, files=["A.md", "B.md"]) == "first rules"
    assert read_project_context(tmp_path, files=["B.md", "A.md"]) == "second rules"


def test_empty_workspace_returns_empty(tmp_path):
    assert read_project_context(tmp_path) == ""


def test_caps_oversized_file_with_truncation_marker(tmp_path):
    (tmp_path / "DEEPDIVE.md").write_text("x" * 500, encoding="utf-8")

    text = read_project_context(tmp_path, max_chars=100)

    assert text.startswith("x" * 100)  # content capped at the budget...
    assert text.endswith("…(truncated)")  # ...with the marker appended (fs_tools convention)


async def test_project_context_renders_into_the_prompt():
    kernel = AgentKernel(FakeLLM([assistant("ok")]), ToolRuntime(), project_context="Use tools.")

    text = render_prompt(await kernel.assembler.assemble({"user_msg": "hi"}))

    assert "Use tools." in text


async def test_empty_project_context_renders_nothing():
    kernel = AgentKernel(FakeLLM([assistant("ok")]), ToolRuntime())

    text = render_prompt(await kernel.assembler.assemble({"user_msg": "hi"}))

    assert "Use tools." not in text


async def test_project_context_feeds_the_cache_identity():
    with_ctx = AgentKernel(FakeLLM([assistant("ok")]), ToolRuntime(), project_context="Use tools.")
    without = AgentKernel(FakeLLM([assistant("ok")]), ToolRuntime())

    await with_ctx.assembler.assemble({"user_msg": "hi"})
    await without.assembler.assemble({"user_msg": "hi"})

    assert with_ctx.assembler.snapshot_key() != without.assembler.snapshot_key()
