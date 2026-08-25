"""Tests for Anthropic-style context prefix enrichment (P1)."""
from core.infrastructure.ingest import Chunk, contextualize_chunks


class _Llm:
    def __init__(self, text: str = "This chunk covers transformers."):
        self.text = text
        self.calls = 0

    async def complete(self, prompt, system):
        self.calls += 1
        return self.text


class _FailingLlm:
    async def complete(self, prompt, system):
        raise RuntimeError("llm down")


async def test_contextualize_prefixes_leaf_chunks():
    chunks = [Chunk(content_en="Attention is all you need.")]
    out = await contextualize_chunks(chunks, "paper.pdf", _Llm("Context: about the paper."))
    assert out[0].content_en.startswith("Context: about the paper.\nAttention")
    assert out[0].meta["raw"] == "Attention is all you need."
    assert out[0].meta["context"] == "Context: about the paper."


async def test_contextualize_skips_parent_chunks():
    parent = Chunk(content_en="big parent text", chunk_kind="parent")
    leaf = Chunk(content_en="small leaf", chunk_kind="leaf")
    llm = _Llm("prefix")
    out = await contextualize_chunks([parent, leaf], "doc", llm)
    # Only the leaf is prefixed; the parent stays untouched and the LLM ran once.
    assert out[0].content_en == "big parent text"
    assert out[1].content_en.startswith("prefix\nsmall leaf")
    assert llm.calls == 1


async def test_contextualize_falls_back_to_raw_on_llm_failure():
    chunks = [Chunk(content_en="raw text")]
    out = await contextualize_chunks(chunks, "doc", _FailingLlm())
    assert out[0].content_en == "raw text"
    assert "raw" not in out[0].meta  # never marked enriched


async def test_contextualize_skips_blank_chunks():
    chunks = [Chunk(content_en="   ")]
    out = await contextualize_chunks(chunks, "doc", _Llm())
    assert out[0].content_en == "   "
