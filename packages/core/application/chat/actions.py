"""Fast-path direct-tool dispatch: an ALLOWLIST over the existing tool registry.

Design invariant (Phase 5 charter): the fast path is a FLOW-CONTROL layer on top of the
capability system — it is NEVER a second capability registry. Every capability a
DIRECT_TOOL can invoke is a tool already registered in the agent's ``ToolRuntime``
(the model itself can call it through the normal loop), so an unrecognized or
partially-parameterized turn loses nothing: it falls through to the Agent + LLM + full
tool/skill/workflow machinery exactly as before the fast path existed.

This module therefore carries ONLY:

  * the allowlist (which registered tools may be dispatched directly);
  * per-tool PARAMETER EXTRACTORS — pure recognizers that turn the user message plus
    already-resolved context facts (attach / owned asset id) into a fully-determined
    argument set. An extractor returns ``None`` whenever anything is undetermined
    (no quoted slot, missing asset reference, a co-occurring second demand, ambiguity
    between two specs) — recognition failure means NO routing, never a guess.
  * :func:`validate_action` — the executor's final schema gate BEFORE the seam.

No execution, no authorization, no I/O here. The side-effect boundary is enforced by
:class:`~core.application.chat.executors.action.ActionExecutor` + the host seam (see
:attr:`ChatDeps.run_tool`): preflight-proven failures may escalate to the Agent; an
exception after the tool was entered is a STATE-UNKNOWN terminal failure and must never
be re-run through the loop (no duplicate folder / duplicate deck).

Phase 5A seeds: ``create_folder`` and ``add_term`` are REGISTERED as real tools first
(``apps/api/tools/*_tool.py``, auto-discovered) so the LLM keeps the same capability as
fallback; plus one already-registered single-shot atomic tool (``pdf_extract_text``)
whose ONLY parameter (the asset id) is fully determined by the request context.
Admission bar (verified per candidate, not by name): single registered atomic tool +
args fully determinable this turn + one deterministic sync/async return — anything
whose product path is a Skill/Workflow or whose tool body hides multi-step generation
stays on the Agent.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field


class ActionSchemaError(Exception):
    """Raised by :func:`validate_action` when a request is malformed (missing / wrong
    type / out-of-bounds slot). Pre-execution ⇒ the executor may safely escalate."""


class ActionPreflightFailure(Exception):
    """Raised by the host seam ONLY when it can GUARANTEE the side effect did not
    happen — an authorization / preflight denial the Agent could clarify with the user.
    Any OTHER exception escaping the seam means the state is UNKNOWN and the executor
    must terminate honestly, never re-run the action through the Agent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ActionIntegrityFailure(Exception):
    """Internal binding / registry inconsistency (C2): unknown tool at the runtime,
    missing action-binding entry, corrupt registry alignment. This is NOT a user
    input problem — the Agent fallback must never be used to "recover" a system
    fault. The executor terminates honestly with a terminal message."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Slot delimiter set (ASCII, curly, CJK quotes). A free-text slot must be quoted — an
# unbounded name/term is dynamically parameterized and belongs on the Agent path.
_QCHARS = '"“”‘’「」『』'
_OPEN = "[" + _QCHARS + "]"
_SLOT = "[^" + _QCHARS + "]+"


def _q(slot: str) -> str:
    """``<quote>slot<quote>`` with one named capture for the slot content."""
    return _OPEN + "(?P<" + slot + ">" + _SLOT + ")" + _OPEN


# Words that mark a SECOND capability demand outside the matched span. A certified
# direct call must be (nearly) the whole request: a compound turn would silently lose
# its second half, so abstain and let the Agent own everything.
_OTHER_DEMAND_PAT = re.compile(
    r"\b(create|delete|save|export|schedule|rename|move|upload|download|run|execute|"
    r"search|browse|web|internet|translate|vocab|vocabulary|glossary|word\s*list)\b|"
    r"(创建|新建|删除|保存|导出|安排|重命名|移动|上传|下载|运行|执行|联网|搜索|查找|"
    r"知识库|笔记|文档|文件|翻译|词库|词汇|单词|生词本)",
    re.IGNORECASE,
)
# Deictic document references ("这篇文档" / "the pdf") carry no parameter — they point
# at the asset the CONTEXT resolves, so they are stripped before the demand check
# ("给这篇文档生成脑图" must NOT abstain merely because "文档" appears).
_DEICTIC_PAT = re.compile(
    r"(?:这|该|那|当前|本|附件里的?|刚上传的?)(?:份|个|篇|张)?\s*"
    r"(?:文档|文件|pdf|ppt|幻灯片|图片|截图|图)"
    r"|(?:this|the|that|attached|current|uploaded)\s+(?:document|doc|file|pdf|deck|image|screenshot|picture)"
    r"|(?:上面|上文|选区里?的?)\s*(?:的)?\s*内容"
    r"|(?:上面|上文|选区里)的?",
    re.IGNORECASE,
)

# ── asset-id resolution from ALREADY-resolved context facts (no I/O) ────────────────

def _asset_id(ctx) -> str | None:
    """The single asset this turn points at (attach or owned upload), else None."""
    owned = getattr(ctx, "owned_asset_id", None)
    if owned:
        return str(owned)
    attach = getattr(getattr(ctx, "body", None), "attach", None)
    if attach is None:
        return None
    if isinstance(attach, dict):
        return str(attach.get("asset_id") or "") or None
    aid = getattr(attach, "asset_id", None)
    return str(aid) if aid else None


def _phrase(pattern: str, *, flags: int = 0) -> re.Pattern:
    return re.compile(pattern, flags)


def _extract_asset_tool(pattern: re.Pattern) -> Callable[[str, object], dict | None]:
    """Extractor factory for tools whose ONLY argument is the context asset id."""

    def extract(text: str, ctx) -> dict | None:
        m = pattern.search(text)
        if not m:
            return None
        rest = (text[: m.start()] + " " + text[m.end():]).strip()
        rest = _DEICTIC_PAT.sub(" ", rest)
        if _OTHER_DEMAND_PAT.search(rest):
            return None  # compound turn → Agent
        asset = _asset_id(ctx)
        if asset is None:
            return None  # parameter undetermined → Agent (LLM fallback intact)
        return {"asset_id": asset}

    return extract


@dataclass(frozen=True)
class DirectToolSpec:
    """One allowlist entry: a REGISTERED tool name, an extractor certifying the turn
    demands exactly that tool with fully-determined args, and the schema gate."""

    tool: str
    description: str
    arg_schema: dict[str, int] = field(default_factory=dict)
    extract: Callable[[str, object], dict | None] | None = None


# ── Seed vocabulary (service actions registered as tools alongside these specs) ─────

def _extract_create_folder(text: str, ctx) -> dict | None:
    for pat in (
        # EN: create/make [me] [a|an|the|new|my]* folder [named|called|titled] "NAME"
        _phrase(
            r"(?:create|make)\s+(?:me\s+)?(?:(?:a|an|the|new|my)\s+)*(?:folder|directory)"
            r"(?:\s+(?:named|called|titled))?\s+" + _q("name"),
            flags=re.IGNORECASE,
        ),
        # ZH: 新建/创建 [一个] 文件夹 "NAME"
        _phrase(r"(?:新建|创建|建立)\s*(?:一个)?\s*文件夹\s*" + _q("name")),
        # ZH: 新建/创建 [一个] 叫|名为|名叫 "NAME" 的文件夹
        _phrase(
            r"(?:新建|创建|建立)\s*(?:一个)?\s*(?:叫|名为|名叫)\s*" + _q("name")
            + r"\s*的\s*(?:文件夹|目录)"
        ),
    ):
        m = pat.search(text)
        if m:
            rest = (text[: m.start()] + " " + text[m.end():]).strip()
            rest = _DEICTIC_PAT.sub(" ", rest)
            if _OTHER_DEMAND_PAT.search(rest):
                return None
            name = (m.group("name") or "").strip()
            return {"name": name} if name else None
    return None


def _extract_add_term(text: str, ctx) -> dict | None:
    for pat in (
        # EN: add "TERM" to (my|the|our) DOMAIN (vocab|vocabulary|word list|glossary)
        _phrase(
            r"add\s+" + _q("term")
            + r"\s+(?:to|in|into)\s+(?:my|the|our)\s+(?P<domain>[\w][\w\s-]*?)"
            r"\s+(?:vocab(?:ulary)?|word\s*list|glossary)",
            flags=re.IGNORECASE,
        ),
        # ZH: 把/将 "TERM" 加入|添加到 [我的] DOMAIN 词汇库|单词库|词库|生词本.
        # (?!我的) makes a MISSING domain fail instead of swallowing the pronoun.
        _phrase(
            r"(?:把|将)\s*" + _q("term")
            + r"\s*(?:加入|添加到|加到|录入)\s*(?:我的|这个)?\s*"
            r"(?P<domain>(?!我的|这个)[\w][\w\s]{0,29}?)"
            r"\s*(?:词汇库|单词库|词库|生词本)"
        ),
    ):
        m = pat.search(text)
        if m:
            rest = (text[: m.start()] + " " + text[m.end():]).strip()
            rest = _DEICTIC_PAT.sub(" ", rest)
            if _OTHER_DEMAND_PAT.search(rest):
                return None
            term = (m.group("term") or "").strip()
            domain = (m.group("domain") or "").strip()
            return {"term": term, "domain": domain} if term and domain else None
    return None


DIRECT_TOOLS: dict[str, DirectToolSpec] = {
    spec.tool: spec
    for spec in (
        DirectToolSpec(
            tool="create_folder",
            description="Create a folder with an explicit quoted name.",
            arg_schema={"name": 120},
            extract=_extract_create_folder,
        ),
        DirectToolSpec(
            tool="add_term",
            description="Add a quoted term to an explicitly-named vocabulary domain.",
            arg_schema={"term": 120, "domain": 60},
            extract=_extract_add_term,
        ),
        # The asset tool below is ALREADY-registered and single-shot atomic; its only
        # parameter (asset_id) is fully determined by the request context — the
        # extractor certifies both the phrase and the asset presence.
        DirectToolSpec(
            tool="pdf_extract_text",
            description="Extract the full text of the attached document.",
            arg_schema={"asset_id": 64},
            extract=_extract_asset_tool(
                _phrase(
                    r"(?:提取|抽取|抽出|转换|转成)[^。;？!]{0,8}(?:全文|文字|文本)"
                    r"|(?:text\s*extract|extract\s+(?:the\s+)?text|get\s+the\s+text)",
                    flags=re.IGNORECASE,
                )
            ),
        ),
        # EXCLUDED from v1, with the verified reason (admission = single-shot atomic
        # tool + fully-determined args + no implicit multi-step):
        #  * ``doc_mindmap`` / ``doc_slides`` — product paths are the toolkit workflows
        #    (``mindmap_gen`` Mermaid / ``slides_gen`` deck engine); the same-named
        #    tools in document_tools.py are downgraded text wrappers.
        #  * ``doc_outline`` — body runs ``extract_document_text``, which itself may
        #    fan out vision-LLM calls on embedded images before the generation call:
        #    not a deterministic single return.
        # Unverified capabilities stay on the Agent path; L0 never claims them.
    )
}


# ── Negation guard (frozen constraint: never execute what the user denied) ──────────
# An explicit negation cue governing one of the allowlisted action verbs MUST abstain
# the whole L0 exact pass: "不要新建文件夹「X」" is a request NOT to create, yet the
# phrase extractor below would happily match the verb+name span. The cue must sit in
# the same clause immediately before the verb (bounded gap, no clause punctuation) so
# trailing "…，但不要删除" style prose does not veto an unrelated positive request.
# Anything the narrow guard is unsure about also abstains — fail-open to the Agent.
_NEGATION_CUE = (
    r"(?:不要|不用|不需要|无需|无须|别|勿|请勿|不准|不可|不得|禁止|不想|不想再|不再"
    r"|don'?t|do not|does not need|never|no need to|not necessary to)"
)
_ACTION_VERB = (
    r"(?:新建|创建|建立|添加|加入|加到|录入|删除|移除|提取|抽取|抽出|转换|保存|导出"
    r"|create|make|add|insert|delete|remove|extract|convert|save|export)"
)
_NEGATION_GUARD = re.compile(
    _NEGATION_CUE + r"[^。；;，,！!？?\n]{0,6}?" + _ACTION_VERB,
    re.IGNORECASE,
)


def is_negated_request(text: str) -> bool:
    """Sentence-level negation in front of an allowlisted action verb. Shared by the
    L0 exact pass and the QIR argument-binding funnel — one guard, two call sites."""
    return bool(_NEGATION_GUARD.search(text or ""))


def match_direct_tool(text: str, ctx) -> dict | None:
    """L0 recognition: the turn demands EXACTLY one allowlisted tool, fully-parameterized.

    Returns a normalized request ``{"tool": …, "args": …}`` or ``None``. ``None`` is
    the LOSSLESS fallback contract: no phrase, a negated request, two matches
    (ambiguity), or a spec whose extractor found undetermined parameters all leave
    the turn on the normal Agent path with its original text and full tool/skill/
    workflow authority.
    """
    if not text:
        return None
    if is_negated_request(text):
        return None  # "不要新建文件夹「X」" must never certify a write
    stripped = text.strip()
    hits: list[tuple[DirectToolSpec, dict[str, str]]] = []
    for spec in DIRECT_TOOLS.values():
        assert spec.extract is not None
        args = spec.extract(stripped, ctx)
        if args:
            hits.append((spec, args))
    if len(hits) != 1:
        return None
    spec, args = hits[0]
    return {"tool": spec.tool, "args": args}


# Back-compatible alias (tests/imports may reference the earlier name).
match_action = match_direct_tool


def bind_arguments(tool_binding: str, text: str, ctx) -> dict | None:
    """Argument Binding layer: ``Capability + raw Query -> structured arguments``.

    This is stage (2) of the frozen plan-resolution pipeline — owned by the action
    binding table, NOT by QIR (which only outputs ``capability_id``). The current
    implementation reuses the existing ``DirectToolSpec.extract`` regex recognizers;
    that is an IMPLEMENTATION CHOICE, not an architectural principle — a future
    constrained argument parser may replace it behind this same function shape.
    Whatever the binding mechanism, its output still must pass ``validate_action``
    and the Runtime governance funnel before anything executes.

    ``None`` means the structured arguments cannot be determined from THIS
    sentence + context (C1: parameters incomplete) — the caller lets the original
    Agent path handle the turn, untouched. A missing binding entry is a registry
    inconsistency (C2) and raises ``ActionIntegrityFailure``.
    """
    if is_negated_request(text):
        return None  # second layer of the negation defense (QIR routed despite L0)
    spec = DIRECT_TOOLS.get(tool_binding)
    if spec is None or spec.extract is None:
        raise ActionIntegrityFailure(f"no argument binding for tool {tool_binding!r}")
    return spec.extract((text or "").strip(), ctx)


def validate_action(tool: str, args: dict) -> dict:
    """Final schema gate (executor, BEFORE the seam): the tool must be on the allowlist,
    every required slot present, str, stripped, within length bounds. Raises
    :class:`ActionSchemaError` — a pre-execution, side-effect-free failure the executor
    may safely escalate (the Agent owns the clarification)."""
    spec = DIRECT_TOOLS.get(tool)
    if spec is None:
        raise ActionSchemaError(f"tool not on the direct-call allowlist: {tool!r}")
    if not isinstance(args, dict):
        raise ActionSchemaError("args must be a mapping")
    out: dict[str, str] = {}
    for slot, max_len in spec.arg_schema.items():
        val = args.get(slot)
        if not isinstance(val, str) or not (val := val.strip()):
            raise ActionSchemaError(f"missing slot: {slot}")
        if len(val) > max_len:
            raise ActionSchemaError(f"slot too long: {slot} (>{max_len})")
        out[slot] = val
    return {"tool": tool, "args": out}
