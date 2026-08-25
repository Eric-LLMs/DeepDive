"""RAG retrieval pipeline: a config-driven executor over pluggable nodes.

The pipeline topology is the configured node name list (see ``pipeline_config``): the
executor creates each enabled node from the registry, runs it in order, and never lets
one node's failure stop the downstream stages. Every node writes a per-node trace so the
admin console can show exactly what each stage produced.

Degradation contract:
- A single node failing (e.g. the embedding service is down) degrades to the surviving
  channels; downstream ranking still runs.
- If *every* ranking channel fails (no rankings were produced at all), :meth:`retrieve`
  raises :class:`RetrievalUnavailable` so callers (e.g. the ``rag_search`` tool) surface
  the "answer from knowledge, don't retry" notice instead of returning a silent empty.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from rag.context import NodeTrace, PipelineContext, RagRequest
from rag.registry import registry
from rag.pipeline_config import RagPipelineConfig

logger = logging.getLogger(__name__)


class RetrievalUnavailable(RuntimeError):
    """Raised when the retrieval stack is entirely down (no ranking channel produced results)."""


@dataclass
class PipelineDeps:
    """Pipeline-level dependencies injected at assembly time (see ``rag.factory``)."""

    embedder: object = None          # EmbeddingPort
    vector_recaller: object = None   # Recaller (semantic)
    keyword_recaller: object = None  # Recaller (tsvector)
    llm: object = None               # LLMPort
    session_factory: object = None
    chunk_repo: object = None        # SqlChunkRepository (parent_expand)


class RAGPipeline:
    def __init__(self, config: RagPipelineConfig, deps: PipelineDeps) -> None:
        self.config = config
        self.deps = deps

    async def retrieve(self, query: str, top_k: int = 5, filters: dict | None = None) -> list[dict]:
        """Retrieval entry point (contract unchanged): returns ``[{id, text, score, meta}]``."""
        result = await self._run(query, top_k, filters)
        return result["hits"]

    async def trace(self, query: str, top_k: int = 5, filters: dict | None = None) -> dict:
        """Run the pipeline and return hits + per-node trace + errors (admin console)."""
        return await self._run(query, top_k, filters)

    async def _run(self, query: str, top_k: int, filters: dict | None) -> dict:
        ctx = PipelineContext(RagRequest(query=query, top_k=top_k, filters=filters))

        for nc in self.config.enabled_nodes:
            try:
                node = registry.create(nc.name, nc.params)
            except KeyError as exc:
                ctx.errors.append(str(exc))
                logger.error("rag pipeline: %s", exc)
                continue
            started = time.perf_counter()
            try:
                status = await node.run(ctx, self.deps)
            except Exception as exc:  # noqa: BLE001 - degrade, never stop the pipeline
                status = "FAIL"
                ctx.errors.append(f"{nc.name}: {exc!r}")
                logger.exception("rag node '%s' failed", nc.name)
            else:
                status = status.value if hasattr(status, "value") else str(status)
            ctx.trace.append(
                NodeTrace(
                    name=node.name,
                    status=status,
                    ms=round((time.perf_counter() - started) * 1000, 2),
                    out=ctx.get_out(node.name),
                )
            )

        # Total recall failure: surface the underlying error so the tool degrades to the
        # "answer from knowledge" notice. Partial failures already produced rankings.
        if not ctx.store.get("rankings") and ctx.errors:
            raise RetrievalUnavailable("; ".join(ctx.errors))

        hits = [h.to_dict() for h in ctx.final_hits()[:top_k]]
        return {"hits": hits, "trace": ctx.trace, "errors": ctx.errors}
