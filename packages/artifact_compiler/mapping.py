"""Deterministic Research-OS graph → artifact source mapping (docs/19 §2 watch-item,
inv. 2 / inv. 11 requirement 6).

Pure dict-in / contracts-out: no LLM, no I/O, no app imports. The mapping is an
*identity transport*, not a re-derivation: an artifact ``evidence_id`` IS the graph
node id, so every citation resolves back to a Research OS graph node in exactly one
lookup and the persisted ``provenance.json`` survives reload unchanged.

Semantic grounding already happened upstream (EVIDENCE stage —
``adjudicate_evidence`` wrote verdicts onto the graph nodes). This module never
re-judges: it only carries Source/Evidence nodes and the verbatim in-text anchors
the manuscript uses into the artifact contract layer.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from artifact_compiler.source import Citation, Evidence, EvidenceType, Locator


def _sha10(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def _iter_nodes(graph: Mapping[str, Any]) -> list[dict]:
    nodes = graph.get("nodes") or {}
    if isinstance(nodes, dict):
        return [n for n in nodes.values() if isinstance(n, dict)]
    return [n for n in nodes if isinstance(n, dict)]


def _evidence_parents(graph: Mapping[str, Any]) -> dict[str, str]:
    """evidence node id → its Source node id (``depends_on`` edges, EVIDENCE-stage
    plumbing; a missing parent degrades to self-sourcing, never to a guess)."""
    parents: dict[str, str] = {}
    for e in graph.get("edges") or []:
        if not isinstance(e, dict) or e.get("kind") != "depends_on":
            continue
        src, dst = str(e.get("src") or ""), str(e.get("dst") or "")
        if src.startswith("ev:") and dst and src not in parents:
            parents[src] = dst
    return parents


def graph_evidence(graph: Mapping[str, Any]) -> dict[str, Evidence]:
    """Artifact Evidence records keyed by graph node id (Source and Evidence nodes).

    ``evidence_type`` is carried as ``fact``: this layer transports, it does not
    classify (classification belongs to the EVIDENCE stage that produced the node).
    """
    parents = _evidence_parents(graph)
    node_ids = {str(n.get("id")) for n in _iter_nodes(graph) if n.get("id")}
    out: dict[str, Evidence] = {}
    for n in _iter_nodes(graph):
        typ, nid = n.get("type"), str(n.get("id") or "")
        if not nid or typ not in ("Source", "Evidence"):
            continue
        url = str(n.get("url") or n.get("canonical_url") or n.get("source_url") or "")
        excerpt = (
            str(n.get("excerpt") or "").strip()
            or str(n.get("label") or "").strip()
            or url or nid
        )
        if typ == "Evidence":
            parent = parents.get(nid, "")
            source_id = parent if parent in node_ids else nid
        else:
            source_id = nid
        out[nid] = Evidence(
            evidence_id=nid,
            source_id=source_id,
            locator=Locator(url=url) if url else Locator(chunk_id=nid),
            excerpt=excerpt[:4000],
            evidence_type=EvidenceType.fact,
        )
    return out


def graph_citations(
    manuscript: str,
    evidence: Mapping[str, Evidence],
) -> tuple[dict[str, str], dict[str, Citation]]:
    """Verbatim marker → citation-id mapping for anchors the manuscript actually
    carries, plus the Citation records. Two tolerated marker spellings per
    evidence, both matched literally (:func:`project_manuscript_to_ast` consumes
    them and attaches the citation id):

    * the ``[src:<digest>]`` / ``[ev:<digest>]`` graph anchor (WRITE convention);
    * the source URL inline in prose (older drafts' ``来源：<url>`` style).

    Evidence with no textual occurrence yields NO citation — the bibliography
    stays honest (a citation nobody references is the v5.2 orphan defect)."""
    markers: dict[str, str] = {}
    citations: dict[str, Citation] = {}
    for ev in sorted(evidence.values(), key=lambda e: e.evidence_id):
        cands: list[str] = []
        anchor = f"[{ev.evidence_id}]"
        if anchor in manuscript:
            cands.append(anchor)
        if ev.locator.url and ev.locator.url in manuscript:
            cands.append(ev.locator.url)
        if not cands:
            continue
        cid = f"cit-{_sha10(ev.evidence_id)}"
        for lit in cands:
            markers.setdefault(lit, cid)  # first claim wins — deterministic order
        citations[cid] = Citation(citation_id=cid, evidence_ids=[ev.evidence_id])
    return markers, citations


def provenance_map(evidence: Mapping[str, Evidence]) -> dict[str, Any]:
    """The persisted artifact↔graph bridge: identity mapping plus each record's
    source node and URL. Reload-safe (plain JSON scalars)."""
    return {
        "mapping": "artifact_evidence_id == graph_node_id",
        "evidence": {
            eid: {
                "graph_node_id": eid,
                "source_node_id": ev.source_id,
                "url": ev.locator.url,
                "sha256": hashlib.sha256(eid.encode("utf-8")).hexdigest()[:10],
            }
            for eid, ev in sorted(evidence.items())
        },
    }
