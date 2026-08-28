"""Workspace source loading + token budgeting for the toolkit pipeline.

``load_sources`` validates, reads, and text-extracts a set of workspace files, then hands
the result to ``budget_plan`` which applies the token budget. When the combined input would
blow the context window, ``budget_plan`` runs a **map-reduce**: each source is condensed to
a citation-preserving digest (over-budget files are first split into line-tracked chunks
and each chunk digested), then the digests are merged into one grounded input for the final
generation stage.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from core.config import settings
from core.infrastructure.ingest import UnsupportedFileType, extract_document_text

from .errors import SourceError

# Map-reduce guardrails: a single over-budget file is split into line-tracked chunks; the
# digest calls run under a concurrency cap, and the chunk count is bounded so a pathological
# file fails loudly instead of burning the whole worker timeout on serial LLM calls.
_MAP_CONCURRENCY = 4
_MAX_MAP_CHUNKS = 24
_CHUNK_CHARS = 9000

_MAP_SYSTEM = (
    "You are an excerpt summarizer for a map-reduce pipeline. Condense the excerpt into a "
    "compact digest that keeps every distinct fact that could matter for a summary, mind "
    "map, or slide deck. When you reference a fact, mark its source location as "
    "[filename:start-end] exactly as shown in the prompt. Never invent content or line "
    "numbers. Output only the digest text."
)

_REDUCE_SYSTEM = (
    "You are a document digest merger. Merge the per-file digests into one coherent digest "
    "for downstream generation. Keep every distinct fact and preserve the [filename:lines] "
    "markers exactly as given. Output only the merged digest text."
)


@dataclass
class WorkspaceSource:
    """One validated, text-extracted input file."""

    name: str            # file name (used for [name:line] citations)
    path: str            # workspace path as passed by the caller
    text: str
    char_count: int
    line_count: int


def token_count(text: str) -> int:
    """Estimate tokens for ``text``; tiktoken when available, else a CJK-aware heuristic.

    tiktoken needs a one-time encoding download, which may be unavailable offline — the
    heuristic undercounts CJK slightly, so ``toolkit_max_input_tokens`` keeps headroom.
    """
    try:
        import tiktoken  # lazy: optional dependency, offline-safe fallback below

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text, disallowed_special=()))
    except Exception:  # noqa: BLE001 - any tiktoken hiccup falls back to the heuristic
        cjk = sum(1 for c in text if "\u2e80" <= c <= "\u9fff" or "\u3000" <= c <= "\u303f")
        other = len(re.sub(r"\s", "", text)) - cjk
        return cjk + max(1, other // 4)


def _split_with_lines(text: str, chunk_chars: int) -> list[tuple[str, int, int]]:
    """Split ``text`` into ``(chunk, start_line_1based, end_line_1based)`` slices.

    Breaks prefer paragraph/newline boundaries near the window end so the chunk's line range
    stays meaningful; the ranges are what the map digest annotates as ``[file:start-end]``.
    """
    if not text.strip():
        return []
    if len(text) <= chunk_chars:
        return [(text, 1, text.count("\n") + 1)]
    chunks: list[tuple[str, int, int]] = []
    start, line_no = 0, 1
    while start < len(text):
        end = min(start + chunk_chars, len(text))
        window = text[start:end]
        cut = None
        para = window.rfind("\n\n")
        if para > 0:
            cut = para + 2
        else:
            nl = window.rfind("\n")
            if nl > 0:
                cut = nl + 1
        if cut is None:
            cut = len(window)
        piece = text[start : start + cut]
        chunks.append((piece, line_no, line_no + piece.count("\n")))
        line_no += piece.count("\n")
        start += cut
    return chunks


async def _digest(src: WorkspaceSource, piece: str, start_line: int, end_line: int, llm) -> str:
    """Condense one excerpt to a citation-marked digest (the ``map`` step)."""
    prompt = (
        f"File: {src.name} (excerpt covering original lines {start_line}-{end_line}).\n"
        f"[{src.name}:{start_line}-{end_line}]\n{piece}"
    )
    return await llm.complete(prompt, _MAP_SYSTEM)


async def _reduce(digests: list[tuple[str, str]], llm) -> str:
    """Merge map digests into one grounded input (the ``reduce`` step)."""
    joined = "\n\n".join(f"[{name}]\n{text}" for name, text in digests)
    if sum(token_count(d) for _, d in digests) <= settings.toolkit_max_input_tokens:
        return joined
    return await llm.complete(
        "Merge the following per-file digests into one coherent digest, keeping every "
        "distinct fact and preserving the [filename:lines] markers exactly.\n\n" + joined,
        _REDUCE_SYSTEM,
    )


async def budget_plan(sources: list[WorkspaceSource], llm) -> list[WorkspaceSource]:
    """Return the sources unchanged if they fit the budget, else their map-reduced digest.

    The reduced result is a single :class:`WorkspaceSource` (name ``"merged"``) that the
    generation stage consumes exactly like a normal source — it just carries the condensed,
    citation-marked text instead of raw file content.
    """
    if sum(token_count(s.text) for s in sources) <= settings.toolkit_max_input_tokens:
        return sources

    sem = asyncio.Semaphore(_MAP_CONCURRENCY)
    digests: list[tuple[str, str]] = []

    async def map_one(src: WorkspaceSource) -> None:
        async with sem:
            if token_count(src.text) <= settings.toolkit_max_input_tokens:
                digests.append((src.name, await _digest(src, src.text, 1, src.line_count, llm)))
                return
            chunks = _split_with_lines(src.text, _CHUNK_CHARS)
            if len(chunks) > _MAX_MAP_CHUNKS:
                raise SourceError(
                    f"{src.name} is too large for map-reduce (limit {_MAX_MAP_CHUNKS} chunks)"
                )
            for piece, start_line, end_line in chunks:
                digests.append(
                    (f"{src.name}:{start_line}-{end_line}", await _digest(src, piece, start_line, end_line, llm))
                )

    results = await asyncio.gather(*(map_one(s) for s in sources), return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException):
            raise SourceError(f"map-reduce failed: {res}") from res

    merged = await _reduce(digests, llm)
    if not merged.strip():
        raise SourceError("map-reduce produced an empty digest")
    return [
        WorkspaceSource(
            name="merged",
            path="",
            text=merged,
            char_count=len(merged),
            line_count=merged.count("\n") + 1,
        )
    ]


async def load_sources(workspace: Path, paths: list[Path], llm) -> list[WorkspaceSource]:
    """Validate, read, and extract text for every input file, then apply the token budget.

    ``paths`` must already be resolved inside ``workspace`` (the pipeline's validate stage
    guarantees this); a re-check keeps the seam safe if called directly.
    """
    root = workspace.resolve()
    sources: list[WorkspaceSource] = []
    for p in paths:
        p = p.resolve()
        if not p.is_relative_to(root):
            raise SourceError(f"path escapes workspace: {p}")
        if not p.is_file():
            raise SourceError(f"not a file: {p}")
        if p.stat().st_size > settings.toolkit_max_file_bytes:
            raise SourceError(f"file too large (>{settings.toolkit_max_file_bytes} bytes): {p.name}")

        data = await asyncio.to_thread(p.read_bytes)
        try:
            text = await extract_document_text(data, p.name, llm)
        except UnsupportedFileType as exc:
            raise SourceError(f"unsupported format for '{p.name}': {exc}") from exc
        except Exception as exc:
            raise SourceError(f"could not extract text from '{p.name}': {exc}") from exc

        if not text.strip():
            continue  # a file with no extractable text is skipped, not fatal
        sources.append(
            WorkspaceSource(
                name=p.name,
                path=str(p),
                text=text,
                char_count=len(text),
                line_count=text.count("\n") + 1,
            )
        )

    if not sources:
        raise SourceError("no extractable text in the selected files")
    return await budget_plan(sources, llm)
