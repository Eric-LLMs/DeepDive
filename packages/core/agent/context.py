"""Context assembly: system prompt + memory + relevant skills + history."""
from core.agent.memory.base import MemoryStore
from core.agent.skills import SkillRegistry


class ContextBuilder:
    def __init__(
        self,
        memory: MemoryStore | None = None,
        skills: SkillRegistry | None = None,
        system_prompt: str = "You are a helpful assistant.",
    ) -> None:
        self.memory = memory
        self.skills = skills
        self.system_prompt = system_prompt

    async def build(
        self,
        user_msg: str,
        history: list[dict] | None = None,
        memory_keys: list[str] | None = None,
    ) -> list[dict]:
        """Assemble the full messages for this turn: system (with memory/skills) + history + the current question."""
        parts = [self.system_prompt]

        if self.memory and memory_keys:
            for key in memory_keys:
                content = await self.memory.load(key)
                if content:
                    parts.append(f"\n## 记忆:{key}\n{content}")

        if self.skills:
            relevant = self.skills.relevant(user_msg)
            if relevant:
                block = "\n\n".join(
                    f"### skill:{s.name}\n{s.instructions}" for s in relevant
                )
                parts.append(f"\n## 可用技能(按需遵循):\n{block}")

        messages: list[dict] = [{"role": "system", "content": "\n".join(parts)}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_msg})
        return messages
