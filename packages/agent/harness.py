"""Testing and demo utilities: a scripted LLM that drives the Agent loop offline, without a real API."""
import json


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


def tool_call(tool_id: str, name: str, args: dict) -> dict:
    """Build a tool-call response (to feed a FakeLLM script)."""
    return {"content": None, "tool_calls": [{"id": tool_id, "name": name, "arguments": json.dumps(args)}]}


def assistant(content: str) -> dict:
    """Build a plain-text assistant response."""
    return {"content": content, "tool_calls": []}
