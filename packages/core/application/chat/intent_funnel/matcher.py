"""Node 1 — Matcher: deterministic hits from the Registry table ONLY (8.1).

Zero in-node business rules: the literals come from ``patterns``/``aliases`` of
the ACTIVE registry version (``re:``-prefixed literals compile to regex, every
other literal is an exact normalized phrase). Global guards (negation,
research/handoff veto) live in the funnel common layer, not here — 8.1-a ruling.

Escalation discipline (8.1): no hit -> MISS (falls to Recall); one hit -> HIT;
several capabilities hit -> MATCH_AMBIGUOUS carrying ALL candidate ids upward for
Judge/Decision to adjudicate. The Matcher itself never picks.

P1 status: SHADOW consumer only (并存迁移) — funnel.route logs this node's verdict
next to the legacy L0 regex outcome; deleting L0 is a P2 decision after
equivalence is measured.
"""
from __future__ import annotations

import logging
import re

from core.application.chat.intent_funnel.registry.types import (
    RE_PREFIX,
    STATUS_ACTIVE,
)

from .contract import MATCH_AMBIGUOUS, MATCH_HIT, MATCH_MISS, MatchResult

logger = logging.getLogger(__name__)

# (version, fingerprint) -> compiled index; the fingerprint key makes staleness
# impossible: a swapped active version is a different cache entry by construction.
_INDEX_CACHE: dict[tuple[int, str], tuple[dict, list]] = {}
_CACHE_MAX = 16


def _norm(s: str) -> str:
    return s.strip().casefold()


def build_index(view) -> tuple[dict, list]:
    """literal -> {capability_id}, plus [(compiled regex, capability_id)].

    Only routable entries (enabled AND status active) are indexed — ruling 4:
    a disabled capability is not a candidate for ANY node, deterministic
    included."""
    key = (view.version, view.fingerprint)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    exact: dict[str, set[str]] = {}
    regexes: list[tuple[re.Pattern, str]] = []
    for e in view.entries:
        if not (e.enabled and e.status == STATUS_ACTIVE):
            continue
        for lit in (*e.patterns, *e.aliases):
            lit = str(lit).strip()
            if not lit:
                continue
            if lit.startswith(RE_PREFIX):
                try:
                    regexes.append((re.compile(lit[len(RE_PREFIX):]), e.capability_id))
                except re.error as exc:
                    # The publish gate rejects broken re: patterns; surviving one
                    # here means an older payload — skip it, never crash routing.
                    logger.warning("matcher: bad regex %r on %s ignored: %r", lit, e.capability_id, exc)
            else:
                exact.setdefault(_norm(lit), set()).add(e.capability_id)
    if len(_INDEX_CACHE) >= _CACHE_MAX:
        _INDEX_CACHE.clear()
    built = (exact, regexes)
    _INDEX_CACHE[key] = built
    return built


def match(query: str, view) -> MatchResult:
    """One deterministic pass over the active version's table. Cost: a dict
    lookup + a handful of regex searches — this node is allowed to be cheap enough
    to run on every shadowed turn."""
    if not (query or "").strip():
        return MatchResult(state=MATCH_MISS, registry_version=view.fingerprint)
    exact, regexes = build_index(view)
    hits: set[str] = set()
    sources: dict[str, list[str]] = {}  # cid -> the literals that produced its hit

    def _note(cid: str, literal: str) -> None:
        hits.add(cid)
        sources.setdefault(cid, []).append(literal)

    q = _norm(query)
    if q in exact:
        for cid in exact[q]:
            _note(cid, q)
    for rx, cid in regexes:
        if rx.search(query):
            _note(cid, f"{RE_PREFIX}{rx.pattern}")
    if not hits:
        return MatchResult(state=MATCH_MISS, registry_version=view.fingerprint)
    if len(hits) == 1:
        cid = next(iter(hits))
        # dedupe (a cap can match via several literals), keep order, bound the
        # log line — regex sources can be long
        literal = ";".join(dict.fromkeys(sources[cid]))[:160]
        return MatchResult(
            state=MATCH_HIT, capability_id=cid,
            registry_version=view.fingerprint, matched_literal=literal,
        )
    return MatchResult(
        state=MATCH_AMBIGUOUS, candidates=tuple(sorted(hits)),
        registry_version=view.fingerprint,
    )
