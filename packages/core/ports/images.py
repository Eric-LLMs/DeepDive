"""Image port: fetch contextual images for a term."""
from typing import Protocol


class ImagePort(Protocol):
    async def fetch(self, word: str, definition: str, context: str, regenerate: bool = False) -> list[str]:
        """Scrape + download contextual images, returning URL paths (e.g. /images/xxx.jpg)."""
        ...
