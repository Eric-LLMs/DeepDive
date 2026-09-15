"""Workspace source loading for the toolkit pipeline.

``load_sources`` validates, reads, and text-extracts a set of workspace files and ALWAYS
returns the full raw text — the toolkit never digests, trims, or rewrites its input
(2026-09-15 directive: the old map-reduce pre-pass handed a generated summary to the
generator as its sole input, lost the detail the deck engine's line-anchored provenance
needs, and timed out on long PDFs; it is removed for good).

``toolkit_max_input_tokens`` is a pure capacity CHECK of the complete input, not a
compression trigger:

- at or below it   → the generator receives the complete raw text in ONE call;
- above it         → the pipeline enters the EXPLICIT big-document multi-call flow planned
  by :func:`plan_big_document`: the raw text is split into line-tracked batches, every
  grounding call sees the RAW text of its own batch, and the structured results are merged
  deterministically (facts / bullets / branches). A document summary is never fed back as
  the sole input; the switch is announced in the job log — nothing is compressed silently.

Chat-history compaction (``apply_compaction`` in the memory layer) is a separate mechanism
for conversation context and shares no code with this module. RAG import
(``core.infrastructure.ingest`` + asset_ingest) indexes its own vector chunks and never
passes through here.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from core.config import settings
from core.infrastructure.ingest import UnsupportedFileType, extract_document_text

from .errors import SourceError

# Big-document flow guardrail: 64 batches × the full one-shot capacity is far beyond any
# supported input; past it the job fails loudly instead of burning the worker timeout.
_MAX_BIGDOC_BATCHES = 64


@dataclass
class WorkspaceSource:
    """One validated, text-extracted input file (or one line-tracked batch of it)."""

    name: str            # file name (used for [name:line] citations)
    path: str            # workspace path as passed by the caller
    text: str
    char_count: int
    line_count: int
    line_offset: int = 1  # 1-based line of the ORIGINAL file where ``text`` starts


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
        cjk = sum(1 for c in text if "⺀" <= c <= "鿿" or "　" <= c <= "〿")
        other = len(re.sub(r"\s", "", text)) - cjk
        return cjk + max(1, other // 4)


def total_input_tokens(sources: list[WorkspaceSource]) -> int:
    return sum(token_count(s.text) for s in sources)


def plan_big_document(sources: list[WorkspaceSource]) -> list[list[WorkspaceSource]] | None:
    """``None`` = the complete input fits the one-shot capacity (send it in ONE call).

    Otherwise return the batches of the EXPLICIT big-document multi-call flow. Each batch
    carries RAW source text that fits a single call; a batch never holds two chunks of the
    same file, so a ``[name:line]`` citation in that batch's output maps to exactly one
    original-line offset (see :func:`remap_citations`).
    """
    limit = settings.toolkit_max_input_tokens
    if total_input_tokens(sources) <= limit:
        return None
    batches: list[list[WorkspaceSource]] = []
    for src in sources:
        for unit in _units_within_capacity(src, limit):
            _pack(batches, unit, limit)
    if len(batches) > _MAX_BIGDOC_BATCHES:
        raise SourceError(
            f"input needs {len(batches)} big-document calls, above the "
            f"{_MAX_BIGDOC_BATCHES}-call guard — select fewer/smaller files."
        )
    return batches


def _units_within_capacity(src: WorkspaceSource, limit: int):
    """Yield ``src`` itself when it fits, else its line-aligned raw chunks (never digests).

    A single line longer than the whole budget (typical of some PDF extractions) is
    hard-split by characters at an estimated token boundary — the chunk keeps the line's
    original offset, so every citation in it still names the correct source line.
    """
    if token_count(src.text) <= limit:
        yield src
        return
    cur: list[str] = []
    cur_tokens = 0
    offset = src.line_offset
    for ln in src.text.splitlines(keepends=True):
        t = token_count(ln)
        if t > limit:  # one oversized line: flush, then hard-split it
            if cur:
                piece = "".join(cur)
                yield _chunk(src, piece, offset)
                offset += piece.count("\n")
                cur, cur_tokens = [], 0
            chars_per = max(1, len(ln) // t)  # this line's own chars-per-token estimate
            step = max(1, int(limit * 0.9) * chars_per)
            for i in range(0, len(ln), step):
                yield _chunk(src, ln[i : i + step], offset)
            offset += 1 if ln.endswith("\n") else 0
            continue
        if cur and cur_tokens + t > limit:
            piece = "".join(cur)
            yield _chunk(src, piece, offset)
            offset += piece.count("\n")
            cur, cur_tokens = [], 0
        cur.append(ln)
        cur_tokens += t
    if cur:
        yield _chunk(src, "".join(cur), offset)


def _chunk(src: WorkspaceSource, piece: str, offset: int) -> WorkspaceSource:
    return WorkspaceSource(
        name=src.name, path=src.path, text=piece,
        char_count=len(piece), line_count=piece.count("\n") + 1,
        line_offset=offset,
    )


def _pack(batches: list[list[WorkspaceSource]], unit: WorkspaceSource, limit: int) -> None:
    """First-fit: one unit per original name per batch, batch total within ``limit``."""
    for b in batches:
        if any(x.name == unit.name for x in b):
            continue
        if total_input_tokens(b) + token_count(unit.text) > limit:
            continue
        b.append(unit)
        return
    batches.append([unit])


_CITATION_RE = re.compile(r"\[(?P<name>[^\[\]:]+):(?P<a>\d+)(?:-(?P<b>\d+))?\]")


def remap_citations(obj: object, offsets: dict[str, int]) -> object:
    """Shift ``[name:line]`` / ``[name:start-end]`` markers from batch-relative to absolute.

    ``offsets`` maps a source name to the 1-based original line where that batch's text for
    the name starts; offset 1 (or unknown names) leaves citations untouched. Returns a
    deep-copied structure; only strings change.
    """
    def sub(m: re.Match) -> str:
        off = offsets.get(m.group("name"), 1) - 1
        if off <= 0:
            return m.group(0)
        a = int(m.group("a")) + off
        b = int(m.group("b")) + off if m.group("b") else None
        rng = str(a) if b is None or b == a else f"{a}-{b}"
        return f"[{m.group('name')}:{rng}]"

    if isinstance(obj, str):
        return _CITATION_RE.sub(sub, obj)
    if isinstance(obj, dict):
        return {k: remap_citations(v, offsets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [remap_citations(v, offsets) for v in obj]
    return obj


async def load_sources(workspace: Path, paths: list[Path], llm) -> list[WorkspaceSource]:
    """Validate, read, and extract text for every input file.

    ``paths`` must already be resolved inside ``workspace`` (the pipeline's validate stage
    guarantees this); a re-check keeps the seam safe if called directly. The full extracted
    text is returned untrimmed — chunking/condensing is forbidden on this path (see module
    docstring). ``llm`` is only used by scanned-PDF OCR fallback inside extraction.
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
            max_mb = settings.toolkit_max_file_bytes // (1024 * 1024)
            raise SourceError(f"file too large (max {max_mb} MB): {p.name}")

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
    return sources
