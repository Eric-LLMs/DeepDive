"""TTS port."""
from typing import Protocol


class TTSPort(Protocol):
    async def synthesize(self, text: str) -> str | None:
        """Synthesize text into speech, returns the audio file path; returns None for empty text or on failure."""
        ...
