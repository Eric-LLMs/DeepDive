"""Recaller abstraction: one implementation per recall channel."""
from abc import ABC, abstractmethod

from rag.types import SearchHit


class Recaller(ABC):
    """Interface of a single recall channel."""

    name: str = "base"

    @abstractmethod
    async def recall(
        self,
        query: str,
        query_embedding: list[float] | None,
        top_k: int,
        filters: dict | None = None,
    ) -> list[SearchHit]:
        """Recall the top_k text chunks by query (text) or query_embedding (vector).

        - Semantic recall only uses query_embedding and can ignore query;
        - Keyword recall only uses query and can ignore query_embedding.
        """
        ...
