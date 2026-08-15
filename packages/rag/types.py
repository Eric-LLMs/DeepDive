"""Shared RAG domain types."""
from dataclasses import dataclass


@dataclass
class SearchHit:
    """A recalled text chunk (the carrier used across the recall/fusion/rerank stages)."""

    id: str
    text: str
    score: float
    meta: dict | None = None
    source: str | None = None  # recall channel identifier: vector / keyword

    def to_dict(self) -> dict:
        """Convert to an Agent-serializable dict."""
        return {"id": self.id, "text": self.text, "score": self.score, "meta": self.meta}
