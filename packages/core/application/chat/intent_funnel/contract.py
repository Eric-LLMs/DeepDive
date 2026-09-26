"""Intent Funnel node contracts — the vocabulary every node speaks.

Chain ruling (2026-09-24, single-hop correction): the active chain is
Matcher -> (Recall on MISS/AMBIGUOUS) -> ToolIntentModel (ONE call: capability
selection + argument extraction) -> Binder (normalize/validate only) ->
Execute; every non-COMPLETE outcome exits to the Agent (8.10). The former
recheck hop and the Decision LLM node are removed from the active path —
``ToolIntentVerdict`` is ToolIntentModel's verdict+draft in one object. No node may pass
anything else across the boundary, and per 8.8 a verdict carries routing
metadata ONLY — never an executor, tool instance or authorization bypass.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── Node 1: Matcher (P1 producer) ────────────────────────────────────────────────

MATCH_HIT = "HIT"
MATCH_MISS = "MISS"
MATCH_AMBIGUOUS = "MATCH_AMBIGUOUS"  # prefixed per 8.10; never the bare word


@dataclass(frozen=True)
class TurnFacts:
    """The current turn's settled structured facts — the Matcher contract input
    (ruling 2026-09-24). Product semantics already depend on them: "总结一下"
    targets the session context, "总结这一页" targets ``viewer.current_page``.
    The Matcher sees ONLY these derived facts — never the raw transcript, and it
    never parses history itself (resolution belongs upstream, once per turn).
    The field vocabulary mirrors the arg_slots ``source`` enum (8.1-b)."""

    has_viewer: bool = False
    viewer_asset_id: str = ""
    viewer_current_page: int | None = None
    has_viewer_selection: bool = False
    has_attachment: bool = False
    has_turn_context: bool = False  # a session-bound turn: prior context exists

    @classmethod
    def of(cls, ctx) -> TurnFacts:
        """Build once from the resolved turn context. ``body.viewer`` is the
        request's ViewerPayload (schemas.py); ``attach`` a dict; both may be
        absent on guest/plain turns."""
        body = ctx.body
        viewer = getattr(body, "viewer", None)
        selections = getattr(viewer, "selections", None) or []
        return cls(
            has_viewer=viewer is not None,
            viewer_asset_id=str(getattr(viewer, "asset_id", "") or ""),
            viewer_current_page=getattr(viewer, "page", None),
            has_viewer_selection=bool(selections),
            has_attachment=bool(getattr(body, "attach", None)),
            has_turn_context=bool(getattr(ctx, "session_id", None)),
        )

# ── 8.10 fallback reason codes (prefixed, never bare words) ───────────────────────
# The new cascade's ONLY downward exits. Any of these on a funnel_trace line means
# the turn went to the Agent with the user text BYTE-IDENTICAL (8.10).
# Retired on the new lane (Action-Contract ruling 2026-09-25): an empty
# candidate set must still pass ToolIntentModel, so the funnel never emits this
# reason any more. Constant kept so archived traces/A-B rows stay decodable.
REASON_NO_CANDIDATE = "NO_CANDIDATE"
REASON_RECALL_TIMEOUT = "RECALL_TIMEOUT"
REASON_RECALL_UNAVAILABLE = "RECALL_UNAVAILABLE"
REASON_TOOL_INTENT_REJECT = "TOOL_INTENT_REJECT"
REASON_TOOL_INTENT_UNCERTAIN = "TOOL_INTENT_UNCERTAIN"
REASON_TOOL_INTENT_TIMEOUT = "TOOL_INTENT_TIMEOUT"
REASON_REGISTRY_UNAVAILABLE = "REGISTRY_UNAVAILABLE"
REASON_VERSION_MISMATCH = "REGISTRY_VERSION_MISMATCH"
# P3: the capability's intent kind exists but its rollout gate is closed —
# registering in the table never enables routing (逐开关灰度).
REASON_KIND_DISABLED = "FUNNEL_KIND_DISABLED"
REASON_BIND_MISSING = "BIND_MISSING"
REASON_BIND_AMBIGUOUS = "BIND_AMBIGUOUS"
REASON_BIND_INVALID = "BIND_INVALID"
REASON_CASCADE_TIMEOUT = "CASCADE_TIMEOUT"
REASON_CASCADE_ERROR = "CASCADE_ERROR"


@dataclass(frozen=True)
class MatchResult:
    """Deterministic table match. AMBIGUOUS carries ALL candidate ids and the
    funnel escalates them upward (8.1) — the Matcher never picks one."""

    state: str = MATCH_MISS
    capability_id: str | None = None
    registry_version: str = ""
    candidates: tuple[str, ...] = ()
    # Which pattern/alias actually produced a single HIT (shadow telemetry —
    # the equivalence dataset needs the literal, not just the verdict).
    # Empty for MISS/AMBIGUOUS (no single answer to attribute).
    matched_literal: str = ""


# ── Node 2: Recall (P1 producer; today lives inside qir.semantic) ────────────────


@dataclass(frozen=True)
class Candidate:
    capability_id: str
    score: float
    matched_example: str = ""
    # which stage produced this candidate ("recall" | "matcher_ambiguous"); the
    # ToolIntentModel sees the union of Recall hits and Matcher-AMBIGUOUS escalations (8.1)
    # and must know which ones carry a calibrated cosine score.
    origin: str = "recall"
    # per-hit provenance (live-table ruling 2026-09-26): EVERY recall hit ≥
    # threshold is kept — no MAX/AVG, no per-capability dedup — so a hit
    # carries the exact sentence and table row that produced it.
    query_kind: str = ""        # "standard" | "similar" ("" for matcher origins)
    language: str = ""
    query_id: str = ""          # row id in capability_{standard,similar}_queries
    standard_query_id: str | None = None


@dataclass(frozen=True)
class RecallResult:
    """Top-k evidence only — recall proposes, it never disposes (design §3)."""

    candidates: tuple[Candidate, ...] = ()


# ── Node 3: ToolIntentModel (select_and_extract + extract, ONE call; P2 producer) ───────────────

TOOL_INTENT_CONFIDENT = "CONFIDENT"
TOOL_INTENT_UNCERTAIN = "UNCERTAIN"
TOOL_INTENT_REJECT = "REJECT"


@dataclass(frozen=True)
class ToolIntentVerdict:
    """ToolIntentModel's single-call output: WHICH capability and the argument draft.
    ``arguments`` is the raw extraction from the same reply — the Binder only
    normalizes/validates it; a backend without extraction power (stub) leaves it
    None and the turn exits BIND_MISSING."""

    decision: str = TOOL_INTENT_UNCERTAIN
    capability_id: str | None = None
    rationale: str = field(default="", repr=False)
    arguments: dict | None = None
    # TELEMETRY ONLY (pure additive, Phase E): the raw confidence reported by
    # the model, kept even when the floor turned it into UNCERTAIN, so Shadow
    # evaluation can sweep floors offline. Routing never reads this field.
    confidence: float | None = None


# ── Binder (wired in P0 over the existing actions.bind_arguments) ────────────────

BIND_COMPLETE = "COMPLETE"
BIND_MISSING = "MISSING"
# P2 binder rewrite delivers all four states (8.7): the four states never
# collapse into a plain None — "no arguments" is a STATE, not an absence.
BIND_AMBIGUOUS = "AMBIGUOUS"
BIND_INVALID = "INVALID"

@dataclass(frozen=True)
class BoundArguments:
    state: str
    args: dict | None = None

    @classmethod
    def of(cls, args: dict | None) -> BoundArguments:
        """Adapter over the legacy ``bind_arguments`` return: dict -> COMPLETE,
        None -> MISSING (the existing C1 abstain shape, unchanged)."""
        return cls(BIND_COMPLETE, args) if args is not None else cls(BIND_MISSING, None)

    @property
    def is_complete(self) -> bool:
        return self.state == BIND_COMPLETE and self.args is not None

    @property
    def is_missing(self) -> bool:
        return self.state == BIND_MISSING


# ── Funnel output ────────────────────────────────────────────────────────────────
# The legacy QIR adapter (``IntentVerdict.from_qir`` over ``qir.RouteResult``)
# was deleted with the QIR package (migration 0014): the funnel certifies a
# turn by returning an ACTION TurnRequirements, everything else is the
# fail-open original.
