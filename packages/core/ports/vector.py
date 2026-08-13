"""Vector retrieval port."""
from typing import Protocol


class EmbeddingPort(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returns a vector list with the same length as the input."""
        ...


class VectorStorePort(Protocol):
    async def upsert(
        self, ids: list[str], texts: list[str], embeddings: list[list[float]], meta: list[dict]
    ) -> None:
        """Write/update text chunks and their vectors."""
        ...

    async def search(
        self, query_embedding: list[float], top_k: int = 5, filters: dict | None = None
    ) -> list[dict]:
        """Semantic retrieval, returns [{id, text, score, meta}]."""
        ...
