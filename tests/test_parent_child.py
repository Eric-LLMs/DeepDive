"""Tests for parent/child (small-to-big) hierarchy splitting (P1)."""
from core.infrastructure.ingest import split_hierarchy


def test_single_leaf_no_hierarchy():
    out = split_hierarchy("short text", chunk_chars=200, overlap=20)
    assert len(out) == 1
    assert out[0].chunk_kind == "leaf"
    assert out[0].parent_chunk_id is None


def test_hierarchy_produces_parents_and_linked_leaves():
    text = " ".join(f"section {i}" for i in range(200))  # ~2200 chars
    out = split_hierarchy(text, chunk_chars=200, overlap=20, parent_chars=600)
    parents = [c for c in out if c.chunk_kind == "parent"]
    leaves = [c for c in out if c.chunk_kind == "leaf"]
    assert parents, "expected at least one parent"
    assert len(leaves) >= len(parents)
    # Every leaf references an existing parent id, and every parent has a client id.
    parent_ids = {p.id for p in parents}
    assert all(l.parent_chunk_id in parent_ids for l in leaves)
    assert all(p.id for p in parents)


def test_sibling_leaves_share_a_parent():
    text = " ".join(f"chunk {i}" for i in range(300))  # ~2700 chars
    out = split_hierarchy(text, chunk_chars=150, overlap=15, parent_chars=450)
    leaves = [c for c in out if c.chunk_kind == "leaf"]
    # With 150-char leaves over 2700 chars there are ~20 leaves and ~6 parents; the same
    # parent must be referenced by multiple sibling leaves.
    from collections import Counter

    counts = Counter(l.parent_chunk_id for l in leaves)
    assert max(counts.values()) >= 2


def test_parent_window_is_larger_than_leaf():
    text = " ".join(f"word{i}" for i in range(400))  # ~2800 chars
    out = split_hierarchy(text, chunk_chars=150, overlap=15, parent_chars=600)
    parents = [c for c in out if c.chunk_kind == "parent"]
    leaves = [c for c in out if c.chunk_kind == "leaf"]
    assert all(len(p.content_en) > len(leaves[0].content_en) for p in parents)
