"""Retrieval capability definition (the seam between the API and the retrieval service).

The API's ``rag_search`` tool consumes a :class:`Retriever`; which concrete provider it gets
(in-process ``RAGPipeline`` vs a gRPC client) is decided at assembly time in ``deps`` by the
``retrieval_mode`` setting. The tool itself never knows or cares.
"""
from typing import Protocol


class Retriever(Protocol):
    async def retrieve(self, query: str, top_k: int = 5, filters: dict | None = None) -> list[dict]:
        """Return the top_k hits as ``[{id, text, score, meta}]``."""
        ...
