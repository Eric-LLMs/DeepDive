"""LLM client for a chat-completions HTTP API, with real streaming.

The endpoint, key, and model are read from configuration.
"""
import json
from collections.abc import AsyncIterator

from agent.llm_errors import raise_classified
from openai import AsyncOpenAI

from core.config import settings

# The OpenAI SDK refuses to build a client without credentials, but the real key is loaded
# from the DB (admin panel) at startup. Use a placeholder until ``configure`` supplies it.
_PLACEHOLDER_KEY = "sk-placeholder"


def _wire_tool_call(tc: dict) -> dict:
    """Normalize one tool_call to the OpenAI wire format ``{id, type, function:{name, arguments}}``.

    Strict providers (e.g. DeepSeek) deserialize each tool call with a required ``type``
    discriminator and a ``function`` wrapper; the agent loop stores a compact
    ``{id, name, arguments}`` shape, which they reject with ``missing field 'type'``.
    """
    if "function" in tc:
        return {**tc, "type": tc.get("type") or "function"}
    return {
        "id": tc.get("id"),
        "type": "function",
        "function": {
            "name": tc.get("name"),
            "arguments": tc.get("arguments") or "{}",
        },
    }


def _wire_messages(messages: list[dict]) -> list[dict]:
    """Return a copy of ``messages`` with assistant ``tool_calls`` in OpenAI wire format.

    Only assistant messages carrying ``tool_calls`` are rewritten (into a fresh dict, so the
    caller's list is untouched); everything else passes through as-is. This covers both the
    in-turn replay (step N+1 echoes step N's calls) and history replayed from ``body.history``.
    """
    out = []
    for m in messages:
        m = dict(m)
        tcs = m.get("tool_calls")
        if m.get("role") == "assistant" and tcs:
            m["tool_calls"] = [_wire_tool_call(tc) for tc in tcs]
        out.append(m)
    return out


class OpenAILLM:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.client = AsyncOpenAI(
            base_url=base_url or settings.llm_base_url,
            api_key=api_key or settings.llm_api_key or _PLACEHOLDER_KEY,
        )
        self.model = model or settings.llm_model

    def configure(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> None:
        """Rebuild the client in place so a runtime config change takes effect without a restart."""
        self.client = AsyncOpenAI(
            base_url=base_url or settings.llm_base_url,
            api_key=api_key or settings.llm_api_key or _PLACEHOLDER_KEY,
        )
        if model:
            self.model = model

    @staticmethod
    def _messages(prompt: str, system_prompt: str) -> list[dict]:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    async def complete(
        self,
        prompt: str,
        system_prompt: str = "You are a helpful assistant.",
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> str:
        client = self.client
        if base_url or api_key:
            client = AsyncOpenAI(
                base_url=base_url or settings.llm_base_url,
                api_key=api_key or settings.llm_api_key or _PLACEHOLDER_KEY,
            )
        resp = await client.chat.completions.create(
            model=model or self.model,
            messages=self._messages(prompt, system_prompt),
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()

    async def complete_stream(
        self,
        prompt: str,
        system_prompt: str = "You are a helpful assistant.",
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[str]:
        client = self.client
        if base_url or api_key:
            client = AsyncOpenAI(
                base_url=base_url or settings.llm_base_url,
                api_key=api_key or settings.llm_api_key or _PLACEHOLDER_KEY,
            )
        stream = await client.chat.completions.create(
            model=model or self.model,
            messages=self._messages(prompt, system_prompt),
            temperature=0.3,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[dict]:
        """Stream a chat completion, yielding per-chunk event dicts.

        Events (``{"type": ...}``):
        - ``thinking``: a ``reasoning_content`` delta (provider reasoning, when present);
        - ``content``: a content delta;
        - ``tool_calls``: the fully accumulated tool calls ``[{id, name, arguments}]``
          (empty list when the model made no calls), emitted once at the end of the stream;
        - ``usage``: the provider token counts ``{prompt_tokens, completion_tokens,
          total_tokens}`` (all 0 when the provider omits them).

        ``base_url`` / ``api_key`` optionally route this call through a specific LLM
        channel without mutating the shared client.
        """
        client = self.client
        if base_url or api_key:
            client = AsyncOpenAI(
                base_url=base_url or settings.llm_base_url,
                api_key=api_key or settings.llm_api_key or _PLACEHOLDER_KEY,
            )
        kwargs = {
            "model": model or self.model,
            "messages": _wire_messages(messages),
            "temperature": 0.3,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
        stream = await client.chat.completions.create(**kwargs)

        # Tool-call arguments/name arrive as fragmented deltas keyed by index; accumulate
        # them across chunks, then emit the assembled calls once the stream ends.
        acc: dict[int, dict] = {}
        async for chunk in stream:
            if not chunk.choices:
                if chunk.usage:
                    u = chunk.usage
                    yield {
                        "type": "usage",
                        "data": {
                            "prompt_tokens": u.prompt_tokens or 0,
                            "completion_tokens": u.completion_tokens or 0,
                            "total_tokens": u.total_tokens or 0,
                        },
                    }
                continue
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield {"type": "thinking", "data": reasoning}
            if delta.content:
                yield {"type": "content", "data": delta.content}
            for tc in (delta.tool_calls or []):
                entry = acc.setdefault(tc.index, {"id": tc.id or "", "name": "", "arguments": ""})
                if tc.id:
                    entry["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        entry["name"] += tc.function.name
                    if tc.function.arguments:
                        entry["arguments"] += tc.function.arguments

        if acc:
            ordered = [acc[i] for i in sorted(acc)]
            yield {"type": "tool_calls", "data": ordered}
        else:
            yield {"type": "tool_calls", "data": []}

    async def generate_definition(self, term: str) -> str:
        """Generate an English definition + Chinese translation for a term."""
        prompt = (
            f"Provide a clear, concise English definition and its Chinese translation "
            f"for the term '{term}'."
        )
        return await self.complete(prompt, "You are a helpful dictionary assistant. Output only the definition.")

    async def analyze_syntax(self, sentence: str) -> str:
        """Syntactic/semantic analysis, returns Markdown."""
        prompt = (
            "Please perform a professional syntactic and semantic analysis for the following "
            "sentence, specifically tailored for an industry/technical context.\n"
            f'Sentence: "{sentence}"\n\n'
            "You MUST format your response EXACTLY following this Markdown template "
            "(Do not output markdown codeblock backticks ```):\n\n"
            "### 📖 句子意译\n"
            "(Provide a clear, fluent, and professional Chinese translation here)\n\n"
            "### 🔍 句法结构\n"
            "* **主干结构**: (Extract the core Subject-Verb-Object)\n"
            "* **深度解析**: (Explain clauses, modifiers, long dependencies, or specific grammatical structures clearly)\n\n"
            "### 🔑 行业核心词汇与词组\n"
            "* **[Key Term 1]**: (Explain its specific meaning and role in this technical context)\n"
            "* **[Key Term 2]**: (Explain its specific meaning and role in this technical context)"
        )
        return await self.complete(prompt, "You are an expert English linguist and tech-domain specialist.")

    async def explain_term(self, term: str, context: str) -> dict:
        """Explain a term's meaning in context (full-sentence translation + English definition of the term)."""
        system_prompt = (
            "You are a linguistic expert helper. "
            "Please perform two tasks:\n"
            "1. **Translate the entire context sentence** into natural, fluent Chinese.\n"
            "2. Provide a concise explanation of the **target term's** specific "
            "meaning/usage within this context (in English).\n\n"
            "Output strictly in JSON format with keys:\n"
            "- 'translation': The full Chinese translation of the sentence.\n"
            "- 'explanation': The explanation of the term."
        )
        user_prompt = f"Target Term: {term}\nContext Sentence: {context}"
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=self._messages(user_prompt, system_prompt),
                response_format={"type": "json_object"},
                temperature=0.3,
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as exc:
            raise raise_classified(exc) from exc

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> dict:
        """Conversation with tool calls (used by the Agent loop).

        ``base_url`` / ``api_key`` optionally route this single call through a specific LLM
        channel (e.g. the credential pinned on a user's access token); the shared client is
        never mutated, so concurrent requests can each use their own channel.

        Returns ``{content, tool_calls, usage}`` where ``usage`` is the token counts from the
        provider (``prompt_tokens``/``completion_tokens``/``total_tokens``, all 0 if absent).
        """
        client = self.client
        if base_url or api_key:
            client = AsyncOpenAI(
                base_url=base_url or settings.llm_base_url,
                api_key=api_key or settings.llm_api_key or _PLACEHOLDER_KEY,
            )
        kwargs = {"model": model or self.model, "messages": _wire_messages(messages), "temperature": 0.3}
        if tools:
            kwargs["tools"] = tools
        resp = await client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        usage = resp.usage
        return {
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (msg.tool_calls or [])
            ],
            "usage": {
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0,
            },
        }
