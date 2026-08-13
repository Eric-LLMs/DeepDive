"""Query rewrite: multi-query expansion + HyDE.

Rewrite/expand the user query before recall to improve recall quality:
- Multi-query expansion: have the LLM generate several rewritten variants covering different phrasings.
- HyDE: have the LLM first write a "hypothetical answer", then recall using the hypothetical document's vector (better for semantic retrieval).
"""
import json
from dataclasses import dataclass

from core.ports.llm import LLMPort


@dataclass
class RewriteResult:
    """Rewrite result: a set of recall queries + an optional HyDE hypothetical document."""

    queries: list[str]
    hyde_doc: str | None = None


class QueryRewriter:
    """LLM-driven query rewriting."""

    def __init__(self, llm: LLMPort, n_variants: int = 2, hyde: bool = False) -> None:
        self.llm = llm
        self.n_variants = n_variants
        self.hyde = hyde

    async def rewrite(self, query: str) -> RewriteResult:
        queries = [query]
        hyde_doc: str | None = None

        if self.n_variants > 0:
            queries.extend(await self._multi_query(query))
        if self.hyde:
            hyde_doc = await self._hyde(query)

        return RewriteResult(queries=queries, hyde_doc=hyde_doc)

    async def _multi_query(self, query: str) -> list[str]:
        """Generate several rewritten variants; returns an empty list on failure (does not affect the original query)."""
        system = (
            "You are a search query rewriting assistant. Given a user query, "
            "generate a few alternative phrasings that would help retrieve "
            "relevant passages from a knowledge base. Output ONLY a JSON array of strings."
        )
        prompt = f"Original query: {query}\nGenerate {self.n_variants} alternative search queries."
        raw = await self.llm.complete(prompt, system)
        try:
            parsed = json.loads(_strip_code_fence(raw))
            variants = [str(q).strip() for q in parsed if isinstance(q, str) and str(q).strip()]
            return variants[: self.n_variants]
        except Exception:
            return []

    async def _hyde(self, query: str) -> str | None:
        """Generate a hypothetical document; returns None on failure (fall back to vectorizing the original query)."""
        system = "You are a helpful assistant. Write a short, factual passage that answers the question."
        prompt = f"Question: {query}\nPassage:"
        raw = await self.llm.complete(prompt, system)
        return raw or None


def _strip_code_fence(raw: str) -> str:
    """Strip the ```json ... ``` wrapper commonly emitted by LLMs."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
