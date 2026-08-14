"""LLM client for a chat-completions HTTP API, with real streaming.

The endpoint, key, and model are read from configuration.
"""
import json
from typing import AsyncIterator

from openai import AsyncOpenAI

from core.config import settings


class OpenAILLM:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.client = AsyncOpenAI(
            base_url=base_url or settings.llm_base_url,
            api_key=api_key or settings.llm_api_key,
        )
        self.model = model or settings.llm_model

    @staticmethod
    def _messages(prompt: str, system_prompt: str) -> list[dict]:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    async def complete(self, prompt: str, system_prompt: str = "You are a helpful assistant.") -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=self._messages(prompt, system_prompt),
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()

    async def complete_stream(
        self, prompt: str, system_prompt: str = "You are a helpful assistant."
    ) -> AsyncIterator[str]:
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=self._messages(prompt, system_prompt),
            temperature=0.3,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

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
        except Exception:
            return {"translation": "Error parsing AI response.", "explanation": "LLM call failed."}

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Conversation with tool calls (used by the Agent loop), returns {content, tool_calls}."""
        kwargs = {"model": self.model, "messages": messages, "temperature": 0.3}
        if tools:
            kwargs["tools"] = tools
        resp = await self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        return {
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (msg.tool_calls or [])
            ],
        }
