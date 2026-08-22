"""Tests for importance-weighted file recall and superseded exclusion.

Verifies the OpenClaw-style ``relevance × importance`` scoring: a high-salience memory
surfaces ahead of a lower-salience one even with more keyword matches, superseded entries
are invisible to search, and files written before the fields existed parse with defaults.
"""
from agent.memory.file import FileMemoryStore


async def test_importance_outranks_more_keyword_matches(tmp_path):
    store = FileMemoryStore(tmp_path)
    # low-importance memory matches name + description + body (6 keyword points × 1)
    await store.save("attention", "attention attends to inputs", description="attention", importance=1)
    # high-importance memory matches only description + body (3 points × 9)
    await store.save("core", "attention mechanism", description="attention", importance=9)

    hits = await store.search("attention")

    assert [m.name for m in hits] == ["core", "attention"]


async def test_search_excludes_superseded_entries(tmp_path):
    store = FileMemoryStore(tmp_path)
    await store.save("old-rule", "Prefer X", importance=8)
    await store.mark_superseded("old-rule")
    await store.save("new-rule", "Prefer X now", importance=8)

    assert [m.name for m in await store.search("Prefer")] == ["new-rule"]
    assert [m.name for m in await store.list()] == ["new-rule"]
    # the audit trail file still exists and stays readable directly
    assert (tmp_path / "old-rule.md").is_file()
    assert (await store.load("old-rule")).status == "superseded"


async def test_legacy_files_without_new_fields_parse_with_defaults(tmp_path):
    (tmp_path / "legacy.md").write_text(
        "---\nname: legacy\ndescription: old shape\n---\n\nbody", encoding="utf-8"
    )

    memory = await FileMemoryStore(tmp_path).load("legacy")

    assert memory.importance == 5
    assert memory.status == "active"
    assert memory.supersedes == ""
