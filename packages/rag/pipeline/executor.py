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

import asyncio
import logging
import time
from dataclasses import dataclass

from rag.pipeline.context import NodeTrace, PipelineContext, RagRequest
from rag.pipeline.pipeline_config import RagPipelineConfig
from rag.pipeline.registry import registry

logger = logging.getLogger(__name__)

# The two recall channels are independent readers of the blackboard (they only append
# their per-query rankings), so running a CONFIGURED-CONSECUTIVE pair concurrently is a
# pure execution optimization: same nodes, same registry, same enable/order semantics,
# same per-node trace and degrade behavior, and the SAME ranking order as a sequential
# run (each node's appends are merged back in configured order — RRF tie-breaks like
# "vector wins" stay identical). ACL / tenant filters / cache / trace are untouched:
# filters ride the shared request; the cache wraps retrieve() above the pipeline.
_RECALL_NAMES = frozenset({"vector_recall", "keyword_recall"})


class RetrievalUnavailable(RuntimeError):
    """Raised when the retrieval stack is entirely down (no ranking channel produced results)."""


@dataclass
class PipelineDeps:
    """Pipeline-level dependencies injected at assembly time (see ``rag.pipeline.factory``)."""

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

        nodes = self.config.enabled_nodes
        i = 0
        while i < len(nodes):
            # A consecutive vector_recall + keyword_recall pair runs concurrently
            # (see _RECALL_NAMES); everything else keeps the sequential contract.
            if (
                i + 1 < len(nodes)
                and nodes[i].name in _RECALL_NAMES and nodes[i + 1].name in _RECALL_NAMES
                and nodes[i].name != nodes[i + 1].name
            ):
                await self._run_recall_pair(ctx, nodes[i], nodes[i + 1])
                i += 2
            else:
                await self._run_node(ctx, nodes[i])
                i += 1

        # Total recall failure: surface the underlying error so the tool degrades to the
        # "answer from knowledge" notice. Partial failures already produced rankings.
        if not ctx.store.get("rankings") and ctx.errors:
            raise RetrievalUnavailable("; ".join(ctx.errors))

        hits = [h.to_dict() for h in ctx.final_hits()[:top_k]]
        return {"hits": hits, "trace": ctx.trace, "errors": ctx.errors}

    async def _run_node(self, ctx: PipelineContext, nc) -> None:
        """Sequential single-node contract: create, run, trace; a failure never stops
        the downstream stages."""
        try:
            node = registry.create(nc.name, nc.params)
        except KeyError as exc:
            ctx.errors.append(str(exc))
            logger.error("rag pipeline: %s", exc)
            return
        started = time.perf_counter()
        try:
            status = await node.run(ctx, self.deps)
        except Exception as exc:
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

    async def _run_recall_pair(self, ctx: PipelineContext, first, second) -> None:
        """Run two independent recall channels concurrently.

        Each node executes against a SHADOW context (same request + a private copy of
        the rankings list), then the appends are merged back in CONFIGURED order — so
        the shared blackboard ends exactly where a sequential run would have ended.
        Trace entries and error records are emitted in configured order too, keeping
        the admin console and the RetrievalUnavailable semantics byte-identical.
        """
        created = []
        for nc in (first, second):
            try:
                created.append(registry.create(nc.name, nc.params))
            except KeyError as exc:
                ctx.errors.append(str(exc))
                logger.error("rag pipeline: %s", exc)

        base = list(ctx.get("rankings", []))

        async def one(node):
            shadow = PipelineContext(ctx.request)
            shadow.store = dict(ctx.store)
            shadow.store["rankings"] = list(base)  # private list per node
            started = time.perf_counter()
            try:
                status = await node.run(shadow, self.deps)
            except Exception as exc:
                status = "FAIL"
                logger.exception("rag node '%s' failed", node.name)
                return node, status, f"{node.name}: {exc!r}", started, shadow
            status = status.value if hasattr(status, "value") else str(status)
            return node, status, None, started, shadow

        results = await asyncio.gather(*(one(node) for node in created))
        merged = list(base)
        for node, status, err, started, shadow in results:  # configured order
            if err is not None:
                ctx.errors.append(err)
            merged.extend(shadow.store.get("rankings", [])[len(base):])
            for name, out in shadow._outs.items():
                ctx.set_out(name, out)
            ctx.trace.append(
                NodeTrace(
                    name=node.name,
                    status=status,
                    ms=round((time.perf_counter() - started) * 1000, 2),
                    out=ctx.get_out(node.name),
                )
            )
        ctx.set("rankings", merged)
