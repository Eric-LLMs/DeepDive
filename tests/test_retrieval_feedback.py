"""Retrieval-feedback loop contracts (DB-free).

``_extract_retrieval`` is a pure function over the turn's message list, so it gets
direct unit tests; the persistence side (``messages.meta`` JSONB in the canonical
``migrations/0001_init.sql``) is verified the same contract way as test_rag_feedback.py.
"""
import json
from pathlib import Path

from api.routers.chat import _extract_retrieval
from core.infrastructure.db import MessageModel
from sqlalchemy.dialects.postgresql import JSONB


# ── _extract_retrieval ───────────────────────────────────────────────────────

def _tc(id_, name, query=None):
    args = json.dumps({"query": query}) if query is not None else "{}"
    return {"id": id_, "name": name, "arguments": args}


def _assistant(calls):
    return {"role": "assistant", "tool_calls": calls}


def _tool(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_no_messages_returns_none():
    assert _extract_retrieval(None) is None
    assert _extract_retrieval([]) is None


def test_no_rag_search_returns_none():
    msgs = [
        _assistant([_tc("c1", "web_search", "x")]),
        _tool("c1", json.dumps([{"id": "1", "score": 1.0, "text": "hit"}])),
    ]
    assert _extract_retrieval(msgs) is None


def test_happy_path_hits_and_queries():
    hit = {"id": "a1", "score": 0.87, "text": "some chunk"}
    msgs = [
        _assistant([_tc("c1", "rag_search", "what is RAG")]),
        _tool("c1", json.dumps([hit])),
    ]
    out = _extract_retrieval(msgs)
    assert out == {
        "hits": [{"id": "a1", "score": 0.87, "text": "some chunk"}],
        "queries": ["what is RAG"],
    }


def test_unavailable_payload_skipped():
    msgs = [
        _assistant([_tc("c1", "rag_search", "q")]),
        _tool("c1", "_UNAVAILABLE"),
    ]
    assert _extract_retrieval(msgs) is None


def test_malformed_json_skipped():
    msgs = [
        _assistant([_tc("c1", "rag_search", "q")]),
        _tool("c1", "{not json"),
    ]
    assert _extract_retrieval(msgs) is None


def test_text_block_content_shape():
    hit = {"chunk_id": "c9", "text": "x"}
    msgs = [
        _assistant([_tc("c1", "rag_search", "q")]),
        _tool("c1", [{"text": json.dumps([hit])}]),
    ]
    out = _extract_retrieval(msgs)
    assert out["hits"][0]["id"] == "c9"  # chunk_id fallback
    assert out["hits"][0]["score"] is None


def test_dedupe_by_id_keeps_first():
    msgs = [
        _assistant([
            _tc("c1", "rag_search", "q1"),
            _tc("c2", "rag_search", "q2"),
        ]),
        _tool("c1", json.dumps([{"id": "a", "score": 0.9, "text": "first"}])),
        _tool("c2", json.dumps([{"id": "a", "score": 0.1, "text": "second"},
                                {"id": "b", "score": 0.5, "text": "new"}])),
    ]
    out = _extract_retrieval(msgs)
    ids = [h["id"] for h in out["hits"]]
    assert ids == ["a", "b"]
    assert out["hits"][0]["score"] == 0.9
    assert out["queries"] == ["q1", "q2"]


def test_text_truncated_to_200():
    long = "z" * 500
    msgs = [
        _assistant([_tc("c1", "rag_search", "q")]),
        _tool("c1", json.dumps([{"id": "a", "score": 1, "text": long}])),
    ]
    out = _extract_retrieval(msgs)
    assert len(out["hits"][0]["text"]) == 200


def test_non_dict_hit_entries_ignored():
    msgs = [
        _assistant([_tc("c1", "rag_search", "q")]),
        _tool("c1", json.dumps(["just a string", {"id": "ok", "text": "t"}])),
    ]
    out = _extract_retrieval(msgs)
    assert [h["id"] for h in out["hits"]] == ["ok"]


# ── messages.meta persistence contract ───────────────────────────────────────

def test_message_model_has_meta_jsonb():
    cols = {c.name: c for c in MessageModel.__table__.columns}
    assert "meta" in cols
    assert isinstance(cols["meta"].type, JSONB)
    assert cols["meta"].nullable


def test_meta_column_in_canonical_init():
    root = Path(__file__).resolve().parents[1]
    sql = (root / "migrations" / "0001_init.sql").read_text(encoding="utf-8")
    ddl = sql[sql.index("CREATE TABLE public.messages ("):sql.index(");", sql.index("CREATE TABLE public.messages ("))]
    assert "meta jsonb" in ddl
