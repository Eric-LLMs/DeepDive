"""Tests for memory_save: guardrails, READ-classification, and the fixed return value.

Regression for two bugs in the old path: (1) `save()` raised PermissionError unless
`confirmed=True`, so the tool could never run even though it was advertised; (2) even if it
ran, `FileMemoryStore.save` returns None and the tool dereferenced `memory.name` → error.
"""
import pytest
from agent.memory.file import FileMemoryStore
from agent.memory.service import MemoryService, memory_save_tool
from agent.memory.types import Memory
from agent.tools.tool_permissions import ToolPermission
from agent.tools.definition import classify_permissions


class _FakeRetriever:
    async def search(self, query, top_k=5):
        return []


def _service(tmp_path, *, note_max_chars=4000):
    return MemoryService(
        FileMemoryStore(tmp_path),
        _FakeRetriever(),
        memory_md_path=tmp_path / "MEMORY.md",
        note_max_chars=note_max_chars,
    )


async def test_save_writes_and_returns_memory(tmp_path):
    service = _service(tmp_path)

    memory = await service.save("attention-mechanism", "Attention weights attend to inputs.")

    assert isinstance(memory, Memory)
    assert memory.name == "attention-mechanism"
    assert memory.content == "Attention weights attend to inputs."
    # the file + index line exist on disk
    assert (tmp_path / "attention-mechanism.md").is_file()
    assert "(attention-mechanism.md)" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


async def test_save_accepts_optional_metadata(tmp_path):
    service = _service(tmp_path)

    memory = await service.save(
        "project-timeline",
        "Freeze merges after Thursday.",
        description="release cut",
        type_="project",
    )

    assert memory.description == "release cut"
    assert memory.type == "project"


async def test_save_rejects_bad_name(tmp_path):
    service = _service(tmp_path)
    for bad in ("Uppercase", "has space", "1starts-with-digit", "dot.name", "under_score"):
        with pytest.raises(ValueError, match="kebab-case"):
            await service.save(bad, "body")


async def test_save_rejects_empty_and_oversize_content(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="must not be empty"):
        await service.save("note", "   ")

    small = _service(tmp_path, note_max_chars=10)
    with pytest.raises(ValueError, match="character limit"):
        await small.save("note", "x" * 11)


async def test_save_rejects_unknown_type(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="memory type"):
        await service.save("note", "body", type_="bogus")


def test_memory_save_tool_is_read_classified(tmp_path):
    tool = memory_save_tool(_service(tmp_path))

    assert tool.destructive is False
    assert ToolPermission.READ in classify_permissions(tool)
    assert ToolPermission.WRITE not in classify_permissions(tool)


async def test_memory_save_tool_execute_writes_through(tmp_path):
    tool = memory_save_tool(_service(tmp_path))

    result = await tool.execute({"name": "note", "content": "body"}, exec=None)

    assert result == {"saved": "note"}
    assert (tmp_path / "note.md").is_file()


async def test_save_roundtrips_importance_in_frontmatter(tmp_path):
    service = _service(tmp_path)

    memory = await service.save("key-rule", "Always verify before asserting.", importance=9)

    assert memory.importance == 9
    text = (tmp_path / "key-rule.md").read_text(encoding="utf-8")
    assert "importance: 9" in text


async def test_save_defaults_importance_to_five(tmp_path):
    memory = await _service(tmp_path).save("plain", "note")

    assert memory.importance == 5


async def test_save_rejects_out_of_range_importance(tmp_path):
    service = _service(tmp_path)
    for bad in (0, 11, -1, 5.5):
        with pytest.raises(ValueError, match="importance"):
            await service.save("note", "body", importance=bad)


async def test_save_with_supersedes_marks_old_memory_inactive(tmp_path):
    service = _service(tmp_path)
    await service.save("topic-style", "Prefer bullet lists.", type_="user")
    old_text = (tmp_path / "topic-style.md").read_text(encoding="utf-8")
    assert "status:" not in old_text  # active by default, no status key written

    new = await service.save(
        "topic-style-v2", "Prefer short paragraphs.", type_="user", supersedes="topic-style"
    )

    assert new.supersedes == "topic-style"
    # old file kept as audit trail but marked superseded + unindexed
    assert "status: superseded" in (tmp_path / "topic-style.md").read_text(encoding="utf-8")
    assert "(topic-style.md)" not in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    # the new memory is indexed
    assert "(topic-style-v2.md)" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    # recall/list no longer surface the superseded entry
    store = FileMemoryStore(tmp_path)
    names = [m.name for m in await store.list()]
    assert "topic-style" not in names
    assert "topic-style-v2" in names


async def test_save_rejects_supersedes_of_missing_memory(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="supersedes targets missing memory"):
        await service.save("note", "body", supersedes="ghost")
