"""TTS implementation with content-hash caching + free edge-tts fallback.

Primary path: OpenAI-compatible ``/v1/audio/speech`` (via the configured TTS base URL/key).
Fallback: Microsoft Edge TTS (``edge-tts``), free and key-less, used when the primary path
fails (e.g. the gateway has no TTS channel) or no TTS key is configured.

Cache key = md5(text_<provider-specific suffix>); on a hit the path is returned directly,
avoiding repeated API cost.
"""
import hashlib

from openai import AsyncOpenAI

from core.config import settings


class OpenAITTS:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.client = AsyncOpenAI(
            base_url=base_url or settings.tts_url,
            api_key=api_key or settings.tts_key,
        )
        self.output_dir = settings.audio_cache_path
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _hash(text: str, suffix: str) -> str:
        raw = f"{text}_{suffix}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    async def synthesize(self, text: str) -> str | None:
        if not text or not text.strip():
            return None

        # Primary: OpenAI-compatible TTS (only when a key is configured).
        if settings.tts_key:
            path = await self._synthesize_openai(text)
            if path:
                return path

        # Fallback: free edge-tts (always available, no key required).
        return await self._synthesize_edge(text)

    async def _synthesize_openai(self, text: str) -> str | None:
        suffix = f"{settings.tts_model}_{settings.tts_voice}"
        file_path = self.output_dir / f"{self._hash(text, suffix)}.mp3"
        if file_path.exists():
            return str(file_path)

        try:
            resp = await self.client.audio.speech.create(
                model=settings.tts_model,
                voice=settings.tts_voice,
                input=text,
            )
            resp.stream_to_file(file_path)
            return str(file_path)
        except Exception:
            return None

    async def _synthesize_edge(self, text: str) -> str | None:
        import edge_tts

        suffix = f"edge_{settings.edge_tts_voice}"
        file_path = self.output_dir / f"{self._hash(text, suffix)}.mp3"
        if file_path.exists():
            return str(file_path)

        try:
            await edge_tts.Communicate(text, settings.edge_tts_voice).save(str(file_path))
            return str(file_path)
        except Exception:
            return None
