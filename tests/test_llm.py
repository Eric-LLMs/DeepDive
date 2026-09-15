"""Tests for the OpenAI-compatible LLM client's wire serialization.

Regression for a production 500: the agent loop stores assistant tool calls in a compact
``{id, name, arguments}`` shape, but strict providers (DeepSeek) deserialize every tool call
with a required ``type`` discriminator and a ``function`` wrapper and reject the compact shape
with ``missing field 'type'``. ``_wire_messages`` normalizes before the request is sent.

Second regression (2026-09-15): batch generation must ride a STREAMED wire with thinking
disabled — dashscope cuts non-streaming requests at ~300s while a full-context Pass A is
still legitimately generating.
"""
from core.infrastructure.llm import OpenAILLM, _wire_messages


def test_assistant_shorthand_tool_calls_are_normalized_to_wire_format():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "name": "memory_search", "arguments": "{}"}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "nothing"},
    ]
    out = _wire_messages(messages)
    assert out[2]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "memory_search", "arguments": "{}"},
        }
    ]
    # untouched messages pass through unchanged
    assert out[0] == messages[0]
    assert out[3] == messages[3]
    # caller's list is not mutated (the loop still reads the compact shape for dispatch)
    assert messages[2]["tool_calls"] == [{"id": "call_1", "name": "memory_search", "arguments": "{}"}]


def test_already_wire_format_is_idempotent_and_missing_type_is_filled():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "function": {"name": "echo", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
            ],
        }
    ]
    out = _wire_messages(messages)
    assert out[0]["tool_calls"][0] == {
        "id": "c1",
        "type": "function",
        "function": {"name": "echo", "arguments": "{}"},
    }
    assert out[0]["tool_calls"][1]["type"] == "function"


def test_missing_arguments_defaults_to_empty_object():
    out = _wire_messages(
        [{"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "name": "echo"}]}]
    )
    assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"


# ── streamed batch-generation wire (2026-09-15 regression) ────────────────────

class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _FakeStream:
    def __init__(self, pieces):
        self._pieces = pieces

    def __aiter__(self):
        async def gen():
            for p in self._pieces:
                yield _Chunk(p)
        return gen()


class _FakeCompletions:
    def __init__(self, captured, pieces):
        self.captured = captured
        self.pieces = pieces

    async def create(self, **kwargs):
        self.captured.append(kwargs)
        return _FakeStream(self.pieces)


class _FakeClient:
    def __init__(self, captured, pieces):
        self.chat = type("C", (), {"completions": _FakeCompletions(captured, pieces)})()


def _llm_with_fake(pieces):
    captured: list[dict] = []
    llm = OpenAILLM(api_key="k", base_url="http://x", model="m")
    llm.client = _FakeClient(captured, pieces)
    return llm, captured


async def test_complete_streams_accumulates_and_disables_thinking():
    llm, captured = _llm_with_fake(["Hel", "lo ", "world "])
    out = await llm.complete("p", "s")
    assert out == "Hello world"  # trailing whitespace stripped as before
    kw = captured[0]
    assert kw["stream"] is True
    assert kw["extra_body"] == {"enable_thinking": False}
    assert "response_format" not in kw


async def test_complete_json_streams_json_mode_and_parses_across_chunks():
    llm, captured = _llm_with_fake(['{"a":', ' 1}'])
    out = await llm.complete_json("give me json", "s")
    assert out == {"a": 1}
    kw = captured[0]
    assert kw["stream"] is True
    assert kw["response_format"] == {"type": "json_object"}
    assert kw["extra_body"] == {"enable_thinking": False}
