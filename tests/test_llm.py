"""Tests for the OpenAI-compatible LLM client's wire serialization.

Regression for a production 500: the agent loop stores assistant tool calls in a compact
``{id, name, arguments}`` shape, but strict providers (DeepSeek) deserialize every tool call
with a required ``type`` discriminator and a ``function`` wrapper and reject the compact shape
with ``missing field 'type'``. ``_wire_messages`` normalizes before the request is sent.
"""
from core.infrastructure.llm import _wire_messages


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
