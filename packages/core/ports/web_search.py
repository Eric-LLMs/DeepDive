"""Web search port: fetch external search results for the agent."""
from typing import Protocol


class WebSearchPort(Protocol):
    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Return a list of ``{"title", "url", "snippet"}`` for a query."""
        ...
