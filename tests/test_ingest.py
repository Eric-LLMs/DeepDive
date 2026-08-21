"""Text extraction + chunking for the asset RAG-ingestion pipeline."""
import pytest

from core.infrastructure.ingest import UnsupportedFileType, extract_text, split_chunks


def test_extract_text_plain_text_decoded():
    assert extract_text("hello 世界".encode("utf-8"), "notes.md") == "hello 世界"


def test_extract_text_subtitle_joins_cue_texts():
    srt = (
        "1\n00:00:01,000 --> 00:00:03,000\nFirst line\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\nSecond line\n"
    ).encode("utf-8")
    assert extract_text(srt, "clip.srt") == "First line\nSecond line"


def test_extract_text_unsupported_extension_raises():
    with pytest.raises(UnsupportedFileType):
        extract_text(b"%PDF-1.4", "book.pdf")


def test_split_chunks_short_text_single_chunk():
    assert split_chunks("a few words", chunk_chars=1000, overlap=100) == ["a few words"]


def test_split_chunks_empty_text():
    assert split_chunks("   \n\t ", chunk_chars=100, overlap=20) == []


def test_split_chunks_long_text_overlaps_and_bounds():
    text = "word " * 500  # ~2500 chars
    chunks = split_chunks(text, chunk_chars=300, overlap=30)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)
    # Consecutive chunks share the overlap tail.
    assert text.find(chunks[1]) < text.find(chunks[0]) + 300
