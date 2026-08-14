"""TTS client for the Kokoro-FastAPI service (OpenAI-compatible ``/v1/audio/speech``).

The Kokoro model runs in a separate container; this client only POSTs text and caches the
returned audio locally (content-hash keyed). The API process never loads the model, so
swapping or updating the TTS model never requires an API restart.

Cache key = md5(kokoro_text_<voice>_<model>); on a hit the cached .wav path is returned.
"""
import hashlib

from openai import AsyncOpenAI

from core.config import settings


class TTSClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.client = AsyncOpenAI(
            base_url=base_url or settings.tts_base_url,
            api_key=api_key or settings.tts_api_key,
        )
        self.output_dir = settings.audio_cache_path
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _hash(text: str, suffix: str) -> str:
        # "kokoro" prefix namespaces the local WAV cache so it won't collide with other audio.
        raw = f"kokoro_{text}_{suffix}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    async def synthesize(self, text: str) -> str | None:
        if not text or not text.strip():
            return None

        suffix = f"{settings.tts_voice}_{settings.tts_model}"
        file_path = self.output_dir / f"{self._hash(text, suffix)}.wav"
        if file_path.exists():
            return str(file_path)

        try:
            resp = await self.client.audio.speech.create(
                model=settings.tts_model,
                voice=settings.tts_voice,
                input=text,
                response_format="wav",
            )
            file_path.write_bytes(resp.content)
            return str(file_path)
        except Exception:
            return None
