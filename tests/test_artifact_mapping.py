"""Core tests for the deterministic graph→artifact mapping (docs/19 inv. 11 req. 6).

Pure functions: no I/O, no LLM, no app imports. The contract under test is the
*identity* transport — artifact ``evidence_id`` == graph node id — plus marker
honesty: citations exist only for anchors the manuscript actually carries.
"""
from __future__ import annotations

import hashlib
import json

from artifact_compiler.mapping import graph_citations, graph_evidence, provenance_map

GRAPH = {
    "nodes": {
        "src:aaaa": {
            "id": "src:aaaa", "type": "Source",
            "url": "https://example.org/tomato", "label": "Tomato study",
        },
        "ev:bbbb": {
            "id": "ev:bbbb", "type": "Evidence",
            "excerpt": "Cooking raises lycopene levels.",
        },
        "k:cccc": {"id": "k:cccc", "type": "Claim", "statement": "irrelevant"},
    },
    "edges": [
        {"src": "ev:bbbb", "dst": "src:aaaa", "kind": "depends_on"},
        {"src": "k:cccc", "dst": "ev:bbbb", "kind": "supports"},
    ],
}


def test_graph_evidence_identity_and_parent():
    ev = graph_evidence(GRAPH)
    # Claims are NOT evidence; Source and Evidence nodes both ride through, keyed
    # by their graph node id (the identity mapping).
    assert set(ev) == {"src:aaaa", "ev:bbbb"}
    assert ev["ev:bbbb"].evidence_id == "ev:bbbb"
    assert ev["ev:bbbb"].source_id == "src:aaaa"          # depends_on parent
    assert ev["src:aaaa"].source_id == "src:aaaa"          # self-sourced
    assert ev["src:aaaa"].locator.url == "https://example.org/tomato"
    assert ev["ev:bbbb"].locator.chunk_id == "ev:bbbb"     # no url → chunk id
    assert ev["ev:bbbb"].excerpt == "Cooking raises lycopene levels."


def test_graph_evidence_missing_parent_degrades_to_self_source():
    g = {
        "nodes": [
            {"id": "ev:orphan", "type": "Evidence", "excerpt": "x"},
        ],
        "edges": [],
    }
    ev = graph_evidence(g)  # list-shaped nodes also accepted
    assert ev["ev:orphan"].source_id == "ev:orphan"


def test_graph_evidence_is_deterministic():
    assert graph_evidence(GRAPH) == graph_evidence(GRAPH)


def test_graph_citations_only_for_anchors_present_in_manuscript():
    ev = graph_evidence(GRAPH)
    ms = (
        "# T\n\nCooking raises lycopene [ev:bbbb].\n\n"
        "See https://example.org/tomato for detail.\n"
    )
    markers, citations = graph_citations(ms, ev)
    assert markers["[ev:bbbb]"]
    assert markers["https://example.org/tomato"]
    assert set(citations) == set(markers.values())
    cid = markers["[ev:bbbb]"]
    assert cid == "cit-" + hashlib.sha256(b"ev:bbbb").hexdigest()[:10]
    assert citations[cid].evidence_ids == ["ev:bbbb"]


def test_graph_citations_no_anchor_yields_no_citation():
    ev = graph_evidence(GRAPH)
    markers, citations = graph_citations("# T\n\nProse without any anchor.\n", ev)
    assert markers == {} and citations == {}   # honest bibliography, no orphans


def test_graph_citations_first_claim_wins_per_marker():
    ev = graph_citations("# T\n\n[ev:bbbb]\n", graph_evidence(GRAPH))[1]
    ev2 = graph_citations("# T\n\n[ev:bbbb]\n", graph_evidence(GRAPH))[1]
    assert ev == ev2


def test_provenance_map_is_identity_and_reload_safe():
    ev = graph_evidence(GRAPH)
    doc = provenance_map(ev)
    assert doc["mapping"] == "artifact_evidence_id == graph_node_id"
    assert set(doc["evidence"]) == {"src:aaaa", "ev:bbbb"}
    assert doc["evidence"]["ev:bbbb"]["source_node_id"] == "src:aaaa"
    # JSON round-trip unchanged (persisted provenance survives reload)
    assert json.loads(json.dumps(doc)) == doc
