"""Text extraction + chunking for the asset RAG-ingestion pipeline.

V1 extractors: plain text (``.txt``/``.md``/markdown and friends) and subtitle files
(``.srt``/``.vtt``/``.lrc``). Unsupported extensions raise :class:`UnsupportedFileType` so
the worker can mark the asset ``FAILED`` instead of crashing the job.
"""
from __future__ import annotations

from pathlib import Path

from core.config import settings
from core.infrastructure import media

_SUBTITLE_EXTS = {".srt", ".vtt", ".lrc"}
_TEXT_EXTS = {".txt", ".md", ".markdown", ".text", ".log", ".json", ".csv"}


class UnsupportedFileType(ValueError):
    """Raised when no text extractor exists for the uploaded file type."""


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def extract_text(content: bytes, name: str) -> str:
    """Return searchable plain text for a file's bytes, dispatching on extension."""
    ext = Path(name).suffix.lower()
    text = _decode(content)
    if ext == ".vtt":
        cues = media.parse_vtt_text(text)
    elif ext == ".lrc":
        cues = media.parse_lrc_text(text)
    elif ext == ".srt":
        cues = media.parse_srt_text(text)
    elif ext in _TEXT_EXTS:
        return text
    else:
        raise UnsupportedFileType(f"no text extractor for '{ext or name}'")
    return "\n".join(c.text for c in cues if c.text)


def split_chunks(
    text: str, chunk_chars: int | None = None, overlap: int | None = None
) -> list[str]:
    """Split whitespace-normalized text into overlapping chunks of ``chunk_chars`` chars."""
    chunk_chars = chunk_chars or settings.ingest_chunk_chars
    overlap = overlap or settings.ingest_chunk_overlap
    text = " ".join(text.split())
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]
    step = max(1, chunk_chars - overlap)
    return [
        text[i : i + chunk_chars]
        for i in range(0, len(text), step)
    ]
