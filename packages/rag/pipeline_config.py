"""Pipeline configuration: the list of nodes IS the pipeline topology.

Configuration is stored as the ``app_settings["rag"]`` JSON blob (see config_store);
env settings only seed the defaults on first boot. Editing the node list / params /
order in the admin console is how operators add, remove, or reorder stages without
touching any code — the executor simply follows the list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.config import Settings
from core.config import settings as _env_settings


@dataclass
class NodeConfig:
    name: str
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkingConfig:
    strategy: str = "fixed"      # fixed | paragraph | sentence | semantic
    chunk_chars: int = 1200
    overlap: int = 150


@dataclass
class RagPipelineConfig:
    """The full runtime RAG configuration."""

    nodes: list[NodeConfig] = field(default_factory=list)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    contextual: bool = False     # P1: LLM context prefixes per chunk (Anthropic-style)
    parent_child: bool = False   # P1: parent/leaf hierarchy (small-to-big)
    cjk: bool = False            # P2: jieba CJK keyword channel

    @property
    def enabled_nodes(self) -> list[NodeConfig]:
        return [n for n in self.nodes if n.enabled]

    @classmethod
    def default(cls, s: Settings | None = None) -> RagPipelineConfig:
        """Seed config matching the current env settings (behavior-identical to pre-refactor)."""
        s = s or _env_settings
        return cls(
            nodes=[
                NodeConfig(
                    "query_rewrite",
                    enabled=s.rag_query_rewrite,
                    params={"n_variants": s.rag_multi_query_n, "hyde": s.rag_hyde},
                ),
                NodeConfig("vector_recall"),
                NodeConfig("keyword_recall"),
                NodeConfig("rrf_fusion"),
                NodeConfig("cross_encoder", params={"model_name": s.reranker_model}),
            ],
            chunking=ChunkingConfig(
                strategy="fixed",
                chunk_chars=s.ingest_chunk_chars,
                overlap=s.ingest_chunk_overlap,
            ),
        )

    def to_dict(self) -> dict:
        return {
            "nodes": [
                {"name": n.name, "enabled": n.enabled, "params": n.params} for n in self.nodes
            ],
            "chunking": {
                "strategy": self.chunking.strategy,
                "chunk_chars": self.chunking.chunk_chars,
                "overlap": self.chunking.overlap,
            },
            "contextual": self.contextual,
            "parent_child": self.parent_child,
            "cjk": self.cjk,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> RagPipelineConfig:
        chunking = raw.get("chunking") or {}
        nodes = []
        for n in raw.get("nodes", []):
            nodes.append(
                NodeConfig(
                    name=str(n["name"]),
                    enabled=bool(n.get("enabled", True)),
                    params=dict(n.get("params") or {}),
                )
            )
        return cls(
            nodes=nodes,
            chunking=ChunkingConfig(
                strategy=str(chunking.get("strategy", "fixed")),
                chunk_chars=int(chunking.get("chunk_chars", 1200)),
                overlap=int(chunking.get("overlap", 150)),
            ),
            contextual=bool(raw.get("contextual", False)),
            parent_child=bool(raw.get("parent_child", False)),
            cjk=bool(raw.get("cjk", False)),
        )
