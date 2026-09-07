"""P3-1 deterministic fingerprints + delta-pending, computed as pure functions.

No I/O, no LLM, no network (P3-1 hard constraint 1): ``plugin.py`` feeds these
functions already-loaded ``graph``/``project`` dicts and decides what skips, what
recomputes and what commits.

Two orthogonal fingerprints (design P3 §6 / freeze constraints 3+4):

* ``claim_fingerprint``  — semantic identity of the claim STATEMENT (normalized
  label/statement text + the canonical strength vocabulary). The physical node id is
  deliberately excluded, so id re-mapping (shadow-set renames, merges, exports)
  can never avalanche a content-addressed reuse key.
* ``evidence_fingerprint`` — content identity of the claim's COMMITTED evidence set:
  a sorted, de-duplicated set of per-evidence identity strings built from
  ``{source_url, edge kinds, verdict, sha(facts+excerpt), source char-len/status}``.
  Order-independent by construction (constraint 4): permuting the backing node/edge
  lists never changes the value; only adds/removes or real content changes do.

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
from typing import Any

__all__ = [
    "claim_fingerprint",
    "evidence_fingerprint",
    "compute_pending",
]

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


def claim_fingerprint(node: dict, *, strength_norm: str | None = None) -> str:
    """Semantic fingerprint of a Claim node — physical id NOT included (constraint 3).

    ``strength_norm`` must be the output of the canonical vocabulary normalizer
    (caller passes ``normalize_claim_strength(...) or ""``) so "high" and "supported"
    cannot produce divergent fingerprints for the same statement.
    """
    return _sha(
        normalize_text(node.get("label")),
        normalize_text(node.get("statement")),
        (strength_norm or "").strip().lower(),
    )


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


def compute_pending(
    stored_fp: str | None, current_fp: str, gate_ok: bool
) -> bool:
    """The frozen constraint-2 rule; see module docstring for the three clauses."""
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
