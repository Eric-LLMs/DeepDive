"""TTS client for the Kokoro-FastAPI service (OpenAI-compatible ``/v1/audio/speech``).

The Kokoro model runs in a separate container; this client only POSTs text and caches the
returned audio locally (content-hash keyed). The API process never loads the model, so
swapping or updating the TTS model never requires an API restart.

Cache key = md5(kokoro_text_<voice>_<model>); on a hit the cached .wav path is returned.

Each Kokoro voice is bound to one language pack, so text is auto-routed: CJK text uses the
Chinese voice (:attr:`tts_voice_zh`), anything else the default English voice. Mixed text is
read by the Chinese voice (understandable for an English-learning app with a Chinese UI).
"""
import hashlib
import re

from openai import AsyncOpenAI

from core.config import settings

# CJK Unified Ideographs + Extension A + Compatibility Ideographs.
_HAS_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SENTENCE_END = set("。！？.!?;；\n")
_MAX_SEGMENT = 200  # hard cap so a single Kokoro call stays well under model input limits


def _voice_for(text: str) -> str:
    """Pick the Kokoro voice for a text: Chinese for CJK text, English otherwise."""
    return settings.tts_voice_zh if _HAS_CJK.search(text) else settings.tts_voice


def split_sentences(text: str) -> list[str]:
    """Split ``text`` into speakable sentence chunks.

    Boundaries are sentence-final punctuation (``。！？.!?;；``) and newlines. The delimiter is
    kept with the preceding text; short fragments (stray whitespace/delimiters) are merged into
    the previous segment, and oversized segments are hard-split so each chunk stays under
    :data:`_MAX_SEGMENT` characters.
    """
    parts = re.split(r"([。！？.!?;；\n])", text.strip())
    segs: list[str] = []
    cur = ""
    for piece in parts:
        if not piece:
            continue
        cur += piece
        if piece in _SENTENCE_END and len(cur.strip()) >= 2:
            segs.append(cur.strip())
            cur = ""
    if cur.strip():
        segs.append(cur.strip())
    out: list[str] = []
    for seg in segs:
        while len(seg) > _MAX_SEGMENT:
            out.append(seg[:_MAX_SEGMENT])
            seg = seg[_MAX_SEGMENT:]
        out.append(seg)
    return out


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

        voice = _voice_for(text)
        suffix = f"{voice}_{settings.tts_model}"
        file_path = self.output_dir / f"{self._hash(text, suffix)}.wav"
        if file_path.exists():
            return str(file_path)

        try:
            resp = await self.client.audio.speech.create(
                model=settings.tts_model,
                voice=voice,
                input=text,
                response_format="wav",
            )
            file_path.write_bytes(resp.content)
            return str(file_path)
        except Exception:
            return None

    async def synthesize_segments(self, text: str):
        """Synthesize ``text`` sentence-by-sentence, yielding each cached WAV path in order.

        Sequential streaming: the first sentence completes quickly and can be played back while
        the remaining sentences are still being synthesized, so audio starts long before the
        whole paragraph would. Cache hits return instantly (zero-latency for re-reads).
        """
        if not text or not text.strip():
            return
        for segment in split_sentences(text):
            path = await self.synthesize(segment)
            if path is not None:
                yield path
