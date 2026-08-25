"""Tests for strategy-dispatchable chunking (fixed/paragraph/sentence/semantic)."""
from core.infrastructure.ingest import split_chunks


def test_fixed_strategy_is_default_and_overlaps():
    text = "word " * 500
    chunks = split_chunks(text, chunk_chars=300, overlap=30)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)


def test_short_text_single_chunk_all_strategies():
    for strategy in ("fixed", "paragraph", "sentence", "semantic"):
        assert split_chunks("a few words", chunk_chars=1000, overlap=100, strategy=strategy) == [
            "a few words"
        ]


def test_paragraph_strategy_keeps_paragraph_boundaries():
    text = "First paragraph sentence here.\n\nSecond paragraph, much longer, goes on and on."
    chunks = split_chunks(text, chunk_chars=60, overlap=10, strategy="paragraph")
    # Each paragraph fits a chunk; the boundary is preserved, not broken mid-word.
    assert all(p in c for p, c in zip(["First paragraph", "Second paragraph"], chunks))
    assert "First paragraph" in chunks[0]
    assert "Second paragraph" in chunks[-1]


def test_paragraph_merges_small_adjacent_paragraphs():
    text = "One.\n\nTwo.\n\nThree."
    chunks = split_chunks(text, chunk_chars=100, overlap=10, strategy="paragraph")
    assert chunks == ["One.\n\nTwo.\n\nThree."]


def test_sentence_strategy_splits_on_boundaries():
    text = "First sentence. Second sentence! Third sentence?"
    chunks = split_chunks(text, chunk_chars=30, overlap=5, strategy="sentence")
    assert len(chunks) >= 2
    assert all(("." in c or "!" in c or "?" in c) for c in chunks)


def test_paragraph_oversized_unit_windows_down():
    long = "x" * 500
    text = f"short para one.\n\n{long}\n\nshort para two."
    chunks = split_chunks(text, chunk_chars=100, overlap=10, strategy="paragraph")
    assert all(len(c) <= 100 for c in chunks)
    assert len(chunks) > 1


def test_semantic_falls_back_to_fixed():
    text = "word " * 300
    assert split_chunks(text, chunk_chars=200, overlap=20, strategy="semantic") == split_chunks(
        text, chunk_chars=200, overlap=20, strategy="fixed"
    )
