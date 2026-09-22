"""Understanding Engine: the typed answer to "what does this turn need?".

``TurnRequirements`` is the OUTPUT contract of understanding (and of
:func:`resolve_requirements`); its owner is this module. It describes CAPABILITY
DEMAND ONLY — it has no knowledge of executors, policies or plans (the policy layer
maps demand to a plan; the executors carry it out).

:func:`resolve_requirements` is the in-process L0 signal engine: pure, no I/O, no
LLM. It reads facts the base context has ALREADY resolved (viewer assembly, attach,
research binding, memory trigger words) plus cheap lexical patterns on the raw user
message, and emits a requirement set. It never guesses whether a general-knowledge
question needs the web beyond a small time-sensitivity prefilter — anything it is not
certain about is reported ``ABSTAIN``/``LOW`` so the policy routes it to the full
Agent path (fail-safe, not fail-open).

The optional L1 fast-LLM arbiter (for AMBIGUOUS turns only) is intentionally NOT
wired in Phase 2: it would add a second model call to the very path meant to shed
latency, and the L0 set already covers the clearly-conversational and clearly-
capability-demanding turns it must distinguish. L1 arrives in a later phase behind
its own gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from core.config import settings


class Signal(str, Enum):
    """Certainty of one capability demand. AMBIGUOUS forces L1/agent arbitration."""

    HIGH = "high"
    LOW = "low"
    AMBIGUOUS = "ambiguous"


class Confidence(str, Enum):
    """Overall routing confidence of the requirement set."""

    HIGH = "high"
    LOW = "low"
    AMBIGUOUS = "ambiguous"
    ABSTAIN = "abstain"


class Complexity(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    COMPLEX = "complex"


@dataclass(frozen=True)
class TurnRequirements:
    """What capabilities the user's turn demands (never how to execute them).

    ``requested_action`` carries the *semantic intent* of an action request (a name +
    raw slot hints), NOT an instantiated ActionSpec — validation and authorization
    happen in Pre-flight / the ACTION executor, never here.
    """

    needs_private: Signal = Signal.LOW
    needs_web: Signal = Signal.LOW
    needs_viewer: Signal = Signal.LOW
    needs_action: Signal = Signal.LOW
    needs_memory: bool = False
    requested_action: dict | None = None
    complexity: Complexity = Complexity.LOW
    confidence: Confidence = Confidence.ABSTAIN


# ── L0 signal engine (in-process, no I/O) ──────────────────────────────────────────

# Anything time-sensitive or world-current belongs on the Agent path (which owns the
# ``web_search`` tool), never on a tool-less direct answer. Deliberately narrow: a
# false "needs_web" only costs the fast path, it never misroutes into a stale answer.
_WEB_PAT = re.compile(
    r"\b(today|yesterday|now|current(ly)?|latest|recent(ly)?|this week|this month|"
    r"this year|news|weather|score|price|stock|version|as of|"
    r"今天|昨天|现在|目前|最新|最近|本周|本月|今年|新闻|天气|比分|股价|版本)\b",
    re.IGNORECASE,
)
# Private-knowledge demand: asks the assistant to look inside the user's own corpus.
_PRIVATE_PAT = re.compile(
    r"\b(my|our)\s+\w*\s*(document|documents|doc|pdf|note|notes|file|files|"
    r"library|corpus|knowledge base|knowledgebase)\b|"
    r"我(的|们).{0,6}(文档|资料|笔记|文件|知识库|知识库里)",
    re.IGNORECASE,
)
# Explicit tool/action demand (create, save, delete, export, schedule …).
_ACTION_PAT = re.compile(
    r"\b(create|delete|save|export|schedule|rename|move|upload|download|run|execute)\b|"
    r"(创建|删除|保存|导出|安排|重命名|移动|上传|下载|运行|执行)",
    re.IGNORECASE,
)


def _lex_private(text: str) -> bool:
    return bool(_PRIVATE_PAT.search(text))


def _lex_web(text: str) -> bool:
    return bool(_WEB_PAT.search(text))


def _lex_action(text: str) -> bool:
    return bool(_ACTION_PAT.search(text))


def _memory_trigger(text: str) -> bool:
    lowered = text.lower()
    return any(w in lowered for w in settings.memory_recall_trigger_words)


# Viewer block kinds that are pure TEXT already present in the prompt — a grounded,
# tool-less answer can serve them. ``roi``/``frame`` are excluded: their pixels are NOT
# in the prompt and require the Agent's ``vision`` tool, so a turn with one never
# qualifies here (the media/vision sub-path stays on the Agent, never merged).
_TEXT_VIEWER_KINDS = frozenset({"selection", "page", "subtitle_window", "full_text", "full_subtitles"})


def _viewer_ground_eligible(assembly: dict) -> bool:
    """True when the assembly has ALREADY injected text content the answer lives in.

    Requires ``status == "injected"`` with at least one text-bearing block and NO
    image block — the ``"stub"`` status (document open but nothing injected, model must
    call ``read_document``) deliberately does NOT qualify, so opening a PDF alone can
    never route this turn onto the viewer fast path.
    """
    if not assembly or assembly.get("status") != "injected":
        return False
    blocks = assembly.get("blocks") or []
    if not blocks:
        return False
    for b in blocks:
        kind = getattr(b, "kind", None)
        if kind not in _TEXT_VIEWER_KINDS or getattr(b, "image_asset_id", None):
            return False
    return True


def resolve_requirements(ctx, message: str) -> TurnRequirements:
    """L0: classify one turn from base-context facts + cheap lexical patterns.

    ``ctx`` is a :class:`~core.application.chat.context.ChatTurnContext` (duck-typed to
    avoid an import cycle); only already-resolved fields are read — no I/O is performed
    here. The engine CONFIDENTLY abstains from a fast path whenever any capability could
    plausibly be required. Phase 2's DIRECT needs all demands LOW; Phase 3's VIEWER
    needs the viewer content to be ALREADY injected as text (Open != Inject enforced by
    the ``injected``-only gate in :func:`_viewer_ground_eligible`).
    """
    text = message or ""

    # Hard facts from the resolved context win over lexical guesses.
    viewer = getattr(ctx, "viewer_assembly", None)
    viewer_active = bool(viewer and viewer.get("status") in ("injected", "stub"))
    attach_present = bool(getattr(getattr(ctx, "body", None), "attach", None))
    research_turn = bool(getattr(ctx, "research_turn", False))
    handoff = getattr(ctx, "effective_handoff", None)

    needs_private = Signal.HIGH if attach_present else (
        Signal.HIGH if _lex_private(text) else Signal.LOW
    )
    needs_viewer = Signal.HIGH if viewer_active else Signal.LOW
    needs_action = Signal.HIGH if (research_turn or handoff or _lex_action(text)) else Signal.LOW
    needs_web = Signal.HIGH if _lex_web(text) else Signal.LOW
    needs_memory = _memory_trigger(text)

    # A research/handoff turn is inherently a multi-step chain — force complex so the
    # policy never fast-paths it.
    if research_turn or handoff:
        return TurnRequirements(
            needs_private=needs_private, needs_web=needs_web, needs_viewer=needs_viewer,
            needs_action=Signal.HIGH, needs_memory=needs_memory,
            complexity=Complexity.COMPLEX, confidence=Confidence.LOW,
        )

    from core.application.chat.sanitization import is_pure_user_text, sanitize_for_direct

    # VIEWER (Phase 3): the content is ALREADY on screen and injected as text, and no
    # OTHER capability is demanded — a single grounded pass over the blocks is the whole
    # job. The message need not be short (it references the shown content), only pure.
    if (
        viewer_active
        and is_pure_user_text(text)
        and _viewer_ground_eligible(viewer)
        and needs_private is Signal.LOW
        and needs_web is Signal.LOW
        and needs_action is Signal.LOW
        and not needs_memory
    ):
        return TurnRequirements(
            needs_viewer=Signal.HIGH, complexity=Complexity.LOW, confidence=Confidence.HIGH,
        )

    # LOCAL_RAG (Phase 4): a pure private-corpus QUESTION — the lexical private demand is
    # the SOLE capability and no document is attached. Attach turns deliberately stay
    # out: the attach note routes to ``read_document`` (a precise tool read of a known
    # file), not semantic recall, and its prefixed "[Attached:" text is not a raw query
    # anyway. Any co-occurring web/viewer/action/memory demand also disqualifies — the
    # staged path answers only from the corpus chunks it retrieves (fail-closed).
    if (
        needs_private is Signal.HIGH
        and not attach_present
        and needs_viewer is Signal.LOW
        and needs_web is Signal.LOW
        and needs_action is Signal.LOW
        and not needs_memory
        and is_pure_user_text(text)
    ):
        return TurnRequirements(
            needs_private=Signal.HIGH, complexity=Complexity.MODERATE,
            confidence=Confidence.HIGH,
        )

    # DIRECT eligibility (Phase 2): pure + short + zero capability demand.
    clean = sanitize_for_direct(text, max_chars=settings.chat_direct_max_chars)
    if clean is None:
        # Impure / too long → not a direct candidate; let the Agent read the whole message.
        return TurnRequirements(
            needs_private=needs_private, needs_web=needs_web, needs_viewer=needs_viewer,
            needs_action=needs_action, needs_memory=needs_memory, confidence=Confidence.ABSTAIN,
        )

    demands = (needs_private, needs_viewer, needs_action, needs_web)
    if any(s is Signal.HIGH for s in demands) or needs_memory:
        # Something is needed but which capability is not fully disambiguated here (or it
        # needs a tool: a viewer ``stub``/image, private read, web) — hand the decision to
        # the Agent, which owns tools + recall authority.
        return TurnRequirements(
            needs_private=needs_private, needs_web=needs_web, needs_viewer=needs_viewer,
            needs_action=needs_action, needs_memory=needs_memory, confidence=Confidence.LOW,
        )

    # Short, pure, no capability demand at all → a tool-less answer is safe and fastest.
    return TurnRequirements(
        needs_private=needs_private, needs_web=needs_web, needs_viewer=needs_viewer,
        needs_action=needs_action, needs_memory=False,
        complexity=Complexity.LOW, confidence=Confidence.HIGH,
    )
