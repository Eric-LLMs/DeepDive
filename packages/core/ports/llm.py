"""LLM port: defines the language model capabilities the domain layer needs."""
from typing import AsyncIterator, Protocol


class LLMPort(Protocol):
    async def complete(self, prompt: str, system_prompt: str = "You are a helpful assistant.") -> str:
        """Synchronous completion (returns the full text)."""
        ...

    async def complete_stream(
        self, prompt: str, system_prompt: str = "You are a helpful assistant."
    ) -> AsyncIterator[str]:
        """Streaming completion (produces tokens one by one)."""
        yield ""  # pragma: no cover - only to mark the async generator signature

    async def explain_term(self, term: str, context: str) -> dict:
        """Explain a term's meaning in context, returns {"translation": str, "explanation": str}."""
        ...

    async def generate_definition(self, term: str) -> str:
        """Generate an English definition + Chinese translation for a term."""
        ...

    async def analyze_syntax(self, sentence: str) -> str:
        """Perform syntactic/semantic analysis on a sentence, returns Markdown (paraphrase + syntactic structure + industry vocabulary)."""
        ...
