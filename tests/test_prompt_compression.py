"""Tests for the prompt compression pipeline: per-message snip.

Verifies that oversized message content is trimmed for the LLM request snapshot while the
persistence copy keeps the full text, and that non-string content (e.g. ``tool_calls``) passes
through untouched.
"""
from agent.loop import ReactLoopAgent
from core.config import settings


def test_snip_caps_oversized_message_for_the_request():
    cap = settings.prompt_message_max_chars
    big = "y" * (cap + 500)
    messages = [{"role": "tool", "tool_call_id": "1", "content": big}]

    request_msgs = ReactLoopAgent._snip_messages(messages)

    assert len(request_msgs[0]["content"]) <= cap + len("…(truncated)")
    assert request_msgs[0]["content"].endswith("…(truncated)")
    assert messages[0]["content"] == big  # the persistence copy stays raw


def test_snip_passes_short_content_and_non_string_fields_through():
    messages = [
        {"role": "user", "content": "short"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "name": "x"}]},
    ]

    request_msgs = ReactLoopAgent._snip_messages(messages)

    assert request_msgs[0]["content"] == "short"
    assert request_msgs[1]["content"] is None
    assert request_msgs[1]["tool_calls"] == [{"id": "t1", "name": "x"}]
