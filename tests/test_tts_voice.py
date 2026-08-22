"""Language-aware Kokoro voice routing: CJK text uses the Chinese voice, not the English one.

Each Kokoro voice is bound to one language pack; feeding Chinese text to the English voice
produces garbled, repeated-character audio. ``_voice_for`` picks the right voice so the
client reads Chinese correctly without the caller knowing the language.
"""
from core.config import settings
from core.infrastructure.tts import _voice_for


def test_chinese_text_uses_zh_voice():
    assert _voice_for("什么是注意力机制?") == settings.tts_voice_zh


def test_english_text_uses_default_voice():
    assert _voice_for("what is attention?") == settings.tts_voice


def test_mixed_text_with_cjk_prefers_zh():
    # Mixed sentence (common in this app: English material + Chinese UI) is read by the
    # Chinese voice; embedded English words remain understandable.
    assert _voice_for("attention 机制") == settings.tts_voice_zh
