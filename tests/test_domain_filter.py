"""Tests for the P1 domain filter + P2 CJK keyword path wiring.

Uses a scripted fake session that captures the SQL/params the recaller builds, so the
predicates (domain_id, leaf-only, CJK content_search) are verified without a database.
"""
import asyncio
from types import SimpleNamespace

from agent.engine.decisions import ToolExecution
from agent.engine.runtime import ToolRuntime

from apps.api.tools.rag_search_tool import register
from rag.recall.keyword import KeywordRecaller, _ts_match


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params):
        self.sql = str(stmt)
        self.params = params
        return self

    def all(self):
        return self.rows


class _FakeSessionFactory:
    def __init__(self, rows=None):
        self.rows = rows or [
            SimpleNamespace(id="c1", content_en="attention text", meta={}, score=0.7)
        ]
        self.session = None

    def __call__(self):
        self.session = _FakeSession(self.rows)
        return self.session


def _recall_sql(query: str, filters: dict | None = None):
    """Run the recaller against a fake session and return (sql, params)."""
    factory = _FakeSessionFactory()
    recaller = KeywordRecaller(factory)

    asyncio.run(recaller.recall(query, None, 5, filters))
    return factory.session.sql, factory.session.params


def test_english_path_uses_content_en():
    sql, _ = _recall_sql("attention mechanism")
    assert "content_en" in sql
    assert "content_search" not in sql


def test_cjk_path_uses_content_search():
    sql, params = _recall_sql("什么是注意力机制")
    assert "content_search" in sql
    assert "plainto_tsquery" in sql
    assert "什么" in params["segq"]


def test_domain_filter_appends_domain_id_predicate():
    sql, params = _recall_sql("query", {"user_id": "u1", "domain_id": "d-1"})
    assert "a.domain_id = :domain_id::uuid" in sql
    assert params["domain_id"] == "d-1"


def test_leaf_only_predicate_always_present():
    sql, _ = _recall_sql("query")
    assert "c.chunk_kind = 'leaf'" in sql


def test_visibility_predicate_present_with_user_id():
    sql, params = _recall_sql("query", {"user_id": "u1"})
    assert "asset_visibility" in sql or "workspace_members" in sql
    assert params["uid"] == "u1"


def test_mixed_query_uses_cjk_path():
    match, score, params = _ts_match("混合 English 中文")
    assert "content_search" in match
    assert "什么" not in params.get("segq", "")  # segments the CJK, keeps latin tokens


# ── rag_search tool forwards the domain arg ──
class _Ctx:
    def __init__(self, retriever):
        self._retriever = retriever

    def resolve(self, key):
        assert key == "retrieval"
        return self._retriever


class _RecordingRetriever:
    def __init__(self):
        self.calls = []

    async def retrieve(self, query, top_k, filters):
        self.calls.append((query, top_k, filters))
        return []


async def test_tool_forwards_domain_into_filters():
    retriever = _RecordingRetriever()
    runtime = ToolRuntime()
    register(runtime, _Ctx(retriever), None)
    await runtime.execute(
        ToolExecution(
            call_id="1",
            name="rag_search",
            arguments={"query": "q", "domain": "dom-123"},
        )
    )
    assert retriever.calls[0][2]["domain_id"] == "dom-123"
    assert "user_id" in retriever.calls[0][2]


async def test_tool_without_domain_keeps_user_id_only():
    retriever = _RecordingRetriever()
    runtime = ToolRuntime()
    register(runtime, _Ctx(retriever), None)
    await runtime.execute(
        ToolExecution(
            call_id="1", name="rag_search", arguments={"query": "q"}
        )
    )
    filters = retriever.calls[0][2]
    assert "user_id" in filters
    assert "domain_id" not in filters
