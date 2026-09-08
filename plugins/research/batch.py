"""P3-1 deterministic fingerprints + delta-pending, computed as pure functions.

No I/O, no LLM, no network (P3-1 hard constraint 1): ``plugin.py`` feeds these
functions already-loaded ``graph``/``project`` dicts and decides what skips, what
recomputes and what commits.

Two fingerprints feed the delta-pending rule (design P3 §6 / freeze constraint 4):

* ``evidence_fingerprint`` — content identity of the claim's COMMITTED evidence set:
  a sorted, de-duplicated set of per-evidence identity strings built from
  ``{source_url, edge kinds, verdict, sha(facts+excerpt), source char-len/status}``.
  Order-independent by construction (constraint 4): permuting the backing node/edge
  lists never changes the value; only adds/removes or real content changes do.
* the ``chunk_id`` of :class:`EvidenceChunk` — a content-stable id over the sorted
  claim-id set + budget, so digest batch hints are reproducible across turns.

(The earlier ``claim_fingerprint`` — semantic identity of the claim STATEMENT — was
never wired into the production pending rule, which keys on the claim node id, and
was removed in P3-4 to keep no orphan implementations.)

Pending rule (constraint 2, frozen verbatim):

    pending == True  ⟺  never batch-committed (no stored fingerprint baseline)
                     OR the claim fails the anchored/gate predicate
                     OR evidence_fingerprint != the stored baseline.

    Only a claim that satisfies anchored/gate AND whose fingerprint has not moved is
    skipped. A claim anchored by the legacy single ``verify`` (which never wrote a
    baseline) is therefore pending until the next ``verify_batch`` stamps it — the
    stamping commit is itself a no-op upsert, never a re-derivation of edges.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "evidence_fingerprint",
    "compute_pending",
    "EvidenceChunk",
    "suggest_chunks",
    "MAX_CHUNK_CLAIMS",
    "EXHAUSTED_ATTEMPTS",
    "DEFAULT_BUDGET_TOKENS",
]

# The chunker's per-chunk claim ceiling mirrors verify_batch's commit cap — one chunk
# is exactly one verify_batch call, so the two constants must never drift.
MAX_CHUNK_CLAIMS = 8

# Default adjudication-context budget (tokens) per chunk: the batched claim set plus
# its findings is sized to stay far below the model's context window (P3 constraint:
# deterministic Python chunking, never a full claims×pages Cartesian prompt).
DEFAULT_BUDGET_TOKENS = 4000

# Crude deterministic token estimate: 4 chars ≈ 1 token, plus a fixed per-claim
# allowance covering its findings payload and the verdict/facts output it costs in
# the batched prompt. Constants only — no tokenizer, no clock, no I/O.
_CHARS_PER_TOKEN = 4
_PER_CLAIM_ALLOWANCE = 150

# Edge kinds that carry committed support/contradiction (E1/E2 semantics inherited
# from verify: a "neutral" verdict annotates an existing Evidence node but adds no
# edge — its content change still surfaces through the Evidence verdict hash).
_TICKET_KINDS = ("supports", "contradicts")


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def normalize_text(value: Any) -> str:
    """Whitespace/case-normalized text; non-strings collapse to ``""``."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).lower()


def evidence_fingerprint(graph: dict, claim_id: str) -> str:
    """Order-normalized content fingerprint of one Claim's committed evidence set.

    Reads only the loaded ``graph`` snapshot: claim→Evidence ticket edges, the
    Evidence node content (verdict/facts/excerpt) and the linked Source's content
    facts (char length + content_status). Pure list-order changes never move the
    value (constraint 4); adds/removes or any content change always do.
    """
    nodes = {n.get("id"): n for n in graph.get("nodes", []) if isinstance(n, dict)}
    sources: dict[str, dict] = {}
    for n in graph.get("nodes", []):
        if isinstance(n, dict) and n.get("type") == "Source" and n.get("url"):
            sources.setdefault(n["url"], n)  # first match == verify's reuse rule

    items: list[str] = []
    kinds_by_ev: dict[str, list[str]] = {}
    for e in graph.get("edges", []):
        if (
            isinstance(e, dict)
            and e.get("src") == claim_id
            and e.get("kind") in _TICKET_KINDS
        ):
            kinds_by_ev.setdefault(e.get("dst", ""), []).append(e["kind"])
    for ev_id, kinds in kinds_by_ev.items():
        ev = nodes.get(ev_id)
        if not isinstance(ev, dict):
            continue
        cu = ev.get("source_url") or ""
        src = sources.get(cu) or {}
        content = _sha(
            json.dumps([s for s in (ev.get("facts") or []) if isinstance(s, str)],
                       ensure_ascii=False, sort_keys=True),
            normalize_text(ev.get("excerpt")),
        )
        items.append(
            "|".join(
                (
                    cu,
                    "&".join(sorted(set(kinds))),
                    (ev.get("verdict") or "").strip().lower(),
                    content,
                    str(src.get("full_char_len", "-")),
                    str(src.get("content_status", "-")),
                )
            )
        )
    return _sha(json.dumps(sorted(set(items)), ensure_ascii=False))


# P3-10 stall breaker: consecutive adjudication attempts for the same claim that
# produce no committed ticket AND no evidence_fingerprint growth trip the claim's
# gap to ``evidence_exhausted`` — an honest known gap, never a refutation.
EXHAUSTED_ATTEMPTS = 2


def compute_pending(
    stored_fp: str | None, current_fp: str, gate_ok: bool, terminal: bool = False
) -> bool:
    """The frozen constraint-2 rule; see module docstring for the three clauses.

    P3-10 stall breaker: a claim whose gap is terminal (``evidence_exhausted``)
    leaves the pending set regardless — re-verifying it has already produced no
    committed evidence twice running. "Evidence insufficient" stays a KNOWN GAP,
    never a refutation; it is simply not pending work anymore.
    """
    if terminal:
        return False  # evidence_exhausted: honest gap, stop burning adjudications
    if stored_fp is None:
        return True  # never batch-committed: no baseline exists to compare against
    if not gate_ok:
        return True  # anchored/gate condition not met
    return stored_fp != current_fp  # evidence set materially changed


def gate_ok(claim_node: dict, normalize_strength) -> bool:
    """CLAIM_GATE predicate for a single claim (inherited from ``_claim_checks``).

    ``normalize_strength`` is injected to keep this module import-free of plugin
    internals; the caller passes ``normalize_claim_strength``.
    """
    return bool(claim_node.get("citations")) and normalize_strength(
        claim_node.get("strength")
    ) is not None


# ── deterministic batch chunker (P3-4) ───────────────────────────────────────
@dataclass(frozen=True)
class EvidenceChunk:
    """One logical adjudication batch: a stable, content-derived group of claims.

    ``chunk_id`` is a content-stable hash (never a UUID): the same normalized
    (claim_ids, budget) always yields the same id, so a digest hint survives reloads
    and is comparable across turns. ``claim_ids`` is a fixed ascending-order tuple —
    iteration order of the input can never change it.
    """

    chunk_id: str
    claim_ids: tuple[str, ...]
    budget: int


def _claim_token_estimate(node: dict) -> int:
    """Deterministic per-claim budget cost: statement text + a fixed findings allowance."""
    text = normalize_text(node.get("label")) + normalize_text(node.get("statement"))
    return len(text) // _CHARS_PER_TOKEN + _PER_CLAIM_ALLOWANCE


def _chunk_id(claim_ids: tuple[str, ...], budget: int) -> str:
    return "chk-" + _sha(",".join(claim_ids), str(budget))[:12]


def suggest_chunks(
    pending_claims: Any,
    graph: dict,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> list[EvidenceChunk]:
    """Pack pending claims into verify_batch-sized, budget-bounded chunks — pure code,
    never an LLM planner (P3 constraint 3: deterministic orchestration).

    ``pending_claims`` accepts claim ids or digest rows (``{"id": ...}``); order does
    not matter — ids are de-duplicated and sorted ascending before packing. Greedy
    first-fit over that fixed order: a chunk closes when the next claim would break
    the token budget or the per-chunk ceiling. A single claim that alone exceeds the
    budget still gets its own chunk (liveness: nothing is silently dropped). Output is
    deterministic for identical normalized input: no clock, no RNG, no external state.

    The result is a *hint* for the agent (digest ``chunk_hint``); correctness never
    depends on it — the authority stays in graph + ``_verify_fps`` + ``compute_pending``.
    """
    ids: list[str] = []
    for item in pending_claims or ():
        if isinstance(item, dict):
            cid = item.get("id")
        else:
            cid = item
        if isinstance(cid, str) and cid.strip():
            ids.append(cid.strip())
    ids = sorted(set(ids))
    if not ids:
        return []

    nodes = {
        n.get("id"): n
        for n in graph.get("nodes", [])
        if isinstance(n, dict) and n.get("type") == "Claim"
    }
    budget = max(1, int(budget_tokens))

    chunks: list[EvidenceChunk] = []
    current: list[str] = []
    current_est = 0
    for cid in ids:
        est = _claim_token_estimate(nodes[cid]) if cid in nodes else _PER_CLAIM_ALLOWANCE
        if current and (current_est + est > budget or len(current) >= MAX_CHUNK_CLAIMS):
            chunk_ids = tuple(current)
            chunks.append(EvidenceChunk(_chunk_id(chunk_ids, budget), chunk_ids, budget))
            current, current_est = [], 0
        current.append(cid)
        current_est += est
    if current:
        chunk_ids = tuple(current)
        chunks.append(EvidenceChunk(_chunk_id(chunk_ids, budget), chunk_ids, budget))
    return chunks
