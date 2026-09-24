"""Fast-path direct-tool dispatch: an ALLOWLIST over the existing tool registry.

Design invariant (Phase 5 charter): the fast path is a FLOW-CONTROL layer on top of the
capability system — it is NEVER a second capability registry. Every capability a
DIRECT_TOOL can invoke is a tool already registered in the agent's ``ToolRuntime``
(the model itself can call it through the normal loop), so an unrecognized or
partially-parameterized turn loses nothing: it falls through to the Agent + LLM + full
tool/skill/workflow machinery exactly as before the fast path existed.

Since the 2026-09-24 structure rulings (docs/temp.md 落点对表) this module carries
ONLY the L0 flow-control layer and the governance vocabulary. It is a TERMINAL
leaf: nothing importable from here may reach back into the funnel — funnel and
understanding import this module at their top level, so the moved names below are
served by an EXPLICIT lookup table, never by function-level imports.

  * :func:`match_direct_tool` — the L0 exact pass over the binding table;
  * :func:`is_negated_request` — the global negation guard (the funnel's
    :func:`guardrails.negated` is its public re-export);
  * the executor-governance exceptions (:class:`ActionSchemaError`,
    :class:`ActionPreflightFailure`, :class:`ActionIntegrityFailure`).

The PARAMETER EXTRACTORS + ``DirectToolSpec`` + ``DIRECT_TOOLS`` table moved to
:mod:`core.application.chat.intent_funnel.registry.plugins` (8.1-b roster — DAG
leaf, nothing in the chat layer is imported there); :func:`bind_arguments` and
:func:`validate_action` moved to :mod:`core.application.chat.intent_funnel.binder`
(the Binder node owns argument truth, 8.7). Historic ``from …actions import``
sites keep working through the lazy module ``__getattr__`` façade at the bottom
of this file — late-bound so the module DAG stays acyclic.

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

    The binding table is resolved through the lazy façade below (call time, never
    import time — this module is a terminal leaf of the chat layer)."""
    if not text:
        return None
    if is_negated_request(text):
        return None  # "不要新建文件夹「X」" must never certify a write
    stripped = text.strip()
    hits: list[tuple[str, dict[str, str]]] = []
    for spec in _moved()["DIRECT_TOOLS"].values():
        assert spec.extract is not None
        args = spec.extract(stripped, ctx)
        if args:
            hits.append((spec.tool, args))
    if len(hits) != 1:
        return None
    tool, args = hits[0]
    return {"tool": tool, "args": args}


# Back-compatible alias (tests/imports may reference the earlier name).
match_action = match_direct_tool


# ── moved definitions, kept reachable through this historic import surface ─────────
# ``bind_arguments``/``validate_action`` are DEFINED in the funnel Binder node (8.7);
# ``DIRECT_TOOLS``/``DirectToolSpec``/``PLUGINS`` live in the registry roster (8.1-b).
# The façade is LAZY (module ``__getattr__``): it preserves both the old import sites
# and the monkeypatch seam used by the legacy QIR lane — the funnel's late
# ``from …actions import bind_arguments`` reads this module's attribute at CALL time.

def _moved() -> dict:
    # ORDER MATTERS: resolve the leaf roster BEFORE the binder — the qir
    # snapshot imports DIRECT_TOOLS through this facade while the registry
    # package is still initializing, and binder itself pauses on that import.
    from core.application.chat.intent_funnel.registry import plugins
    out = {
        "DIRECT_TOOLS": plugins.DIRECT_TOOLS,
        "DirectToolSpec": plugins.DirectToolSpec,
        "PLUGINS": plugins.PLUGINS,
    }
    from core.application.chat.intent_funnel import binder
    out.update({
        "bind_arguments": binder.bind_arguments,
        "validate_action": binder.validate_action,
    })
    return out


def __getattr__(name: str):
    moved = _moved()
    if name in moved:
        return moved[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
