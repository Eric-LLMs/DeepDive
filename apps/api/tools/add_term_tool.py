"""``add_term``: add a word to a vocabulary domain addressed BY NAME.

Registers the SAME service entry the REST endpoint uses (``VocabularyService.add_term``),
so the LLM keeps the capability as fallback for the chat fast path's ``add_term`` allowlist
entry. The caller names a domain (``"English"``, ``"英语"``); resolution enforces the
uniqueness contract BEFORE any write:

* ``name.strip().lower()`` exact match over the caller-visible domains
  (public + own — the same visibility set ``list_domains`` exposes);
* exactly 1 match → proceed;
* 0 matches → ``"preflight: … not found"`` (provably no side effect);
* >1 matches → ``"preflight: ambiguous …"`` — NEVER pick ``matches[0]``.

``"preflight: "``-prefixed failures are the channel the ACTION executor maps to
"escalate to the Agent, which may clarify with the user"; a failure escaping AFTER
``svc.add_term`` was entered is state-UNKNOWN and never retried blindly.
"""
from __future__ import annotations

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block
from core.application.services import VocabError, VocabularyService
from core.infrastructure.images import ImageScraper
from core.infrastructure.repositories import (
    SqlDomainRepository,
    SqlMatchRepository,
    SqlSentenceRepository,
    SqlTermRepository,
)
from core.infrastructure.request_context import get_request_user_id
from core.infrastructure.tts import TTSClient


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
    def _service(session) -> VocabularyService:
        # add_term only touches the domain/term repositories; the media ports exist
        # to satisfy the service's composition, exactly like ``deps.get_vocab_service``.
        return VocabularyService(
            SqlDomainRepository(session),
            SqlTermRepository(session),
            SqlSentenceRepository(session),
            SqlMatchRepository(session),
            llm,
            TTSClient(),
            ImageScraper(),
            None,
        )

    async def add_term(args: dict, exec: ToolExecution) -> str:
        uid = get_request_user_id()
        if uid is None:
            raise ValueError("preflight: add_term requires an authenticated user")
        term = (args.get("term") or "").strip()
        domain_name = (args.get("domain") or "").strip()
        definition = (args.get("definition") or "").strip()
        if not term or not domain_name:
            raise ValueError("preflight: both term and domain are required")
        async with ctx.resolve("session_factory")() as session:
            svc = _service(session)
            key = domain_name.strip().lower()
            matches = [
                d for d in await svc.list_domains(uid)
                if (d.name or "").strip().lower() == key
            ]
            if not matches:
                raise ValueError(f"preflight: vocabulary domain {domain_name!r} not found")
            if len(matches) > 1:
                raise ValueError(
                    f"preflight: {len(matches)} vocabulary domains are named "
                    f"{domain_name!r}; ask the user which one"
                )
            domain = matches[0]
            try:
                obj = await svc.add_term(domain.id, term, definition, user_id=uid)
            except VocabError as exc:
                # Visibility gates run before any insert ⇒ provably not executed.
                raise ValueError(f"preflight: {exc}") from None
        return f"Added {obj.word!r} to the {domain.name!r} vocabulary."

    runtime.register(
        define_tool(
            name="add_term",
            description="Add a word to a vocabulary domain named by the user (e.g. "
            "'English', 'physics'). Fails with a 'preflight' error if the domain does not "
            "exist or several visible domains share the name — then ask the user to "
            "disambiguate instead of guessing.",
            parameters={
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "The word to add."},
                    "domain": {
                        "type": "string",
                        "description": "Vocabulary domain name (public or owned by the user).",
                    },
                    "definition": {
                        "type": "string",
                        "description": "Optional short definition for the word.",
                    },
                },
                "required": ["term", "domain"],
            },
            output=ToolOutput(
                schema={"type": "string"}, render=lambda args, value: [text_block(value)]
            ),
            execute=add_term,
        )
    )
