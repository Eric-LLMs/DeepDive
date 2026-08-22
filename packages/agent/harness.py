"""Testing and demo utilities: a scripted LLM that drives the Agent loop offline, without a real API."""
import json

_USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


class FakeLLM:
    """Scripted LLM: pops preset responses in order."""

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls: list[tuple[list[dict], list[dict] | None]] = []

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> dict:
        self.calls.append((list(messages), tools))
        if not self.script:
            return {"content": None, "tool_calls": []}
        return self.script.pop(0)

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        """Yield scripted events: one response per call, decomposed into thinking/content/tool_calls/usage."""
        self.calls.append((list(messages), tools))
        if not self.script:
            response = {"content": None, "tool_calls": []}
        else:
            response = self.script.pop(0)
        thinking = response.get("thinking")
        if thinking:
            yield {"type": "thinking", "data": thinking}
        content = response.get("content")
        if content:
            yield {"type": "content", "data": content}
        yield {"type": "tool_calls", "data": response.get("tool_calls") or []}
        yield {"type": "usage", "data": dict(_USAGE)}


def tool_call(tool_id: str, name: str, args: dict) -> dict:
    """Build a tool-call response (to feed a FakeLLM script)."""
    return {"content": None, "tool_calls": [{"id": tool_id, "name": name, "arguments": json.dumps(args)}]}


def assistant(content: str) -> dict:
    """Build a plain-text assistant response."""
    return {"content": content, "tool_calls": []}


def stream_assistant(thinking: str | None = None, content: str = "") -> dict:
    """Build a scripted response with an optional reasoning fragment (for chat_stream)."""
    return {"thinking": thinking, "content": content, "tool_calls": []}
