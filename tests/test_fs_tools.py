"""Resident fs/shell tools: async reads, atomic edits, bash sandbox wiring."""
import pytest
from agent.tools.bash_sandbox import HostBashSandbox
from agent.tools.fs_tools import bash_tool, edit_file_tool, read_file_tool


async def test_read_file_returns_content(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    tool = read_file_tool(tmp_path)
    assert await tool.execute({"path": "a.txt"}, None) == "hello"


async def test_read_file_rejects_escape(tmp_path):
    tool = read_file_tool(tmp_path)
    with pytest.raises(ValueError, match="escapes workspace"):
        await tool.execute({"path": "../secret.txt"}, None)


async def test_edit_replaces_snippet(tmp_path):
    (tmp_path / "a.txt").write_text("old line\nkeep", encoding="utf-8")
    tool = edit_file_tool(tmp_path)
    out = await tool.execute(
        {"path": "a.txt", "old_text": "old line", "new_text": "new line"}, None
    )
    assert out == "replaced 1 occurrence in a.txt"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "new line\nkeep"


async def test_edit_writes_new_file_atomically(tmp_path):
    tool = edit_file_tool(tmp_path)
    out = await tool.execute({"path": "new.txt", "new_text": "content"}, None)
    assert out == "wrote new.txt"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "content"
    # No leftover temp files after the atomic replace.
    assert [p.name for p in tmp_path.iterdir()] == ["new.txt"]


async def test_edit_missing_old_text_raises(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    tool = edit_file_tool(tmp_path)
    with pytest.raises(ValueError, match="old_text not found"):
        await tool.execute({"path": "a.txt", "old_text": "zzz", "new_text": "x"}, None)


async def test_bash_tool_rejects_escape(tmp_path):
    tool = bash_tool(tmp_path, HostBashSandbox(tmp_path))
    with pytest.raises(ValueError, match="escapes workspace"):
        await tool.execute({"command": "cat ../x"}, None)


async def test_bash_tool_runs_in_sandbox(tmp_path):
    tool = bash_tool(tmp_path, HostBashSandbox(tmp_path))
    out = await tool.execute({"command": "echo hello"}, None)
    assert "hello" in out
