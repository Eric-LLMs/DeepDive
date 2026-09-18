"""STT client for the FunASR SenseVoice sidecar (OpenAI-compatible ``/v1/audio/transcriptions``).

The SenseVoiceSmall model runs in a separate container; this client only POSTs recorded
audio and returns the transcription text. The API process never loads the model, so
swapping or updating the STT model never requires an API restart (same contract as
:mod:`core.infrastructure.tts` for Kokoro).

The sidecar is reached over the host loopback (:data:`settings.stt_base_url`), because the
API gateway itself runs on the host, not inside the compose network.
"""
import io

from openai import AsyncOpenAI

from core.config import settings


class STTClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.client = AsyncOpenAI(
            base_url=base_url or settings.stt_base_url,
            api_key=api_key or settings.stt_api_key,
            timeout=timeout or settings.stt_timeout_seconds,
        )

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        """Transcribe one recorded clip.

        ``content_type`` is passed through as the multipart part MIME so the FunASR server
        can decode by content (webm/opus from the desktop, wav/mp4 from other clients).
        Language is auto-detected by SenseVoice — the desktop mic can receive either
        Chinese or English speech. Errors are raised to the caller; the endpoint maps
        them to HTTP 502. Transcriptions are one-shot (typed by hand each time) so there
        is deliberately no caching.
        """
        resp = await self.client.audio.transcriptions.create(
            model=settings.stt_model,
            file=(filename, io.BytesIO(audio), content_type),
        )
        return (resp.text or "").strip()
