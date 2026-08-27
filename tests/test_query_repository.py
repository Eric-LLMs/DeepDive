"""Query Repository — multi-source import (file / learning / chat) behavior.

Covers the source-aware recall predicates (LEFT JOIN + file-only READY/domain), the shared
``write_query_repo_chunks`` helper, the chat segmentation helpers (default grouping + LLM
fallback), and the .docx / .pdf text-extraction dispatch. Uses scripted fakes — no database.
"""
import asyncio
from types import SimpleNamespace

from apps.worker.tasks import (
    _default_chat_pairs,
    _pending_chat_entries,
    _segment_chat,
    _strip_code_fence,
)
from core.infrastructure.ingest import (
    Chunk,
    extract_document_text,
    extract_text,
    write_query_repo_chunks,
)
from rag.recall.keyword import KeywordRecaller


# ── source-aware recall predicates (scripted fake session, no DB) ──
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
        self.rows = rows or [SimpleNamespace(id="c1", content_en="text", meta={}, score=0.7)]
        self.session = None

    def __call__(self):
        self.session = _FakeSession(self.rows)
        return self.session


def _kw_sql(query="attention", filters=None):
    factory = _FakeSessionFactory()
    asyncio.run(KeywordRecaller(factory).recall(query, None, 5, filters))
    return factory.session.sql, factory.session.params


def test_recall_left_joins_assets_for_non_file_chunks():
    sql, _ = _kw_sql()
    assert "LEFT JOIN assets a ON a.id = c.asset_id" in sql


def test_recall_readiness_applies_only_to_file_chunks():
    sql, _ = _kw_sql("attention", {"user_id": "u1"})
    assert "c.asset_id IS NULL OR a.file_status = 'READY'" in sql


def test_recall_domain_filter_is_file_only():
    sql, params = _kw_sql("attention", {"user_id": "u1", "domain_id": "d1"})
    assert "c.asset_id IS NOT NULL AND a.domain_id = :domain_id::uuid" in sql
    assert params["domain_id"] == "d1"


def test_recall_still_leaf_only():
    sql, _ = _kw_sql()
    assert "c.chunk_kind = 'leaf'" in sql


# ── write_query_repo_chunks: embeds + bulk-inserts with source columns ──
class _FakeEmbedder:
    def __init__(self):
        self.calls = []

    async def embed(self, texts):
        self.calls.append(texts)
        return [[0.1] * 8 for _ in texts]


class _FakeChunkRepo:
    def __init__(self):
        self.bulk_calls = []

    async def bulk_insert(
        self, asset_id, user_id, workspace_id, chunks, source_type="file", source_id=None
    ):
        self.bulk_calls.append((asset_id, user_id, workspace_id, source_type, source_id, chunks))


def test_write_query_repo_chunks_persists_source_type_and_id(monkeypatch):
    from core.infrastructure import drive_repositories

    repo = _FakeChunkRepo()
    monkeypatch.setattr(drive_repositories, "SqlChunkRepository", lambda factory: repo)

    class _Factory:
        pass

    res = asyncio.run(
        write_query_repo_chunks(
            _Factory(),
            _FakeEmbedder(),
            chunks=[Chunk(content_en="hello world")],
            user_id="u1",
            source_type="chat",
            source_id="msg-1",
        )
    )

    assert res == {"chunks": 1}
    asset_id, user_id, workspace_id, source_type, source_id, rows = repo.bulk_calls[0]
    assert asset_id is None
    assert user_id == "u1"
    assert workspace_id is None
    assert source_type == "chat"
    assert source_id == "msg-1"
    assert rows[0]["content_en"] == "hello world"
    assert rows[0]["embedding"] == [0.1] * 8


# ── chat segmentation helpers ──
def _msg(role, text):
    return SimpleNamespace(role=role, text=text)


def test_default_chat_pairs_merge_follow_ups_into_one_answer():
    messages = [
        _msg("user", "What is RRF?"),
        _msg("assistant", "Reciprocal Rank Fusion."),
        _msg("assistant", "It fuses ranks."),
    ]
    pairs = _default_chat_pairs(messages)
    assert pairs == [
        {
            "question": "What is RRF?",
            "answer": "Reciprocal Rank Fusion.\nIt fuses ranks.",
            "indices": [0],
        }
    ]


def test_default_chat_pairs_splits_distinct_questions():
    messages = [
        _msg("user", "Q1"),
        _msg("assistant", "A1"),
        _msg("user", "Q2"),
        _msg("assistant", "A2"),
    ]
    pairs = _default_chat_pairs(messages)
    assert pairs == [
        {"question": "Q1", "answer": "A1", "indices": [0]},
        {"question": "Q2", "answer": "A2", "indices": [2]},
    ]


def test_strip_code_fence():
    assert _strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_segment_chat_parses_llm_json():
    class _GoodLLM:
        async def complete(self, prompt, system):
            # No indices field (older model) → fallback assigns user lines in order.
            return '[{"question": "Q1", "answer": "A1"}, {"question": "Q2", "answer": "A2"}]'

    messages = [_msg("user", "Q1"), _msg("assistant", "A1"), _msg("user", "Q2"), _msg("assistant", "A2")]
    pairs = asyncio.run(_segment_chat(messages, _GoodLLM()))
    assert pairs == [
        {"question": "Q1", "answer": "A1", "indices": [0]},
        {"question": "Q2", "answer": "A2", "indices": [2]},
    ]


def test_segment_chat_keeps_llm_merged_indices():
    class _MergingLLM:
        async def complete(self, prompt, system):
            # The LLM merged two follow-up user turns (indexes 2, 4) into one entry.
            return (
                '[{"question": "What is RRF?", "answer": "A1", "indices": [0]},'
                '{"question": "Explain more", "answer": "A2", "indices": [2, 4]}]'
            )

    messages = [
        _msg("user", "What is RRF?"),
        _msg("assistant", "A1"),
        _msg("user", "Wait, clarify"),
        _msg("assistant", "A2"),
        _msg("user", "And an example?"),
        _msg("assistant", "A3"),
    ]
    pairs = asyncio.run(_segment_chat(messages, _MergingLLM()))
    assert pairs == [
        {"question": "What is RRF?", "answer": "A1", "indices": [0]},
        {"question": "Explain more", "answer": "A2", "indices": [2, 4]},
    ]


def test_segment_chat_degrades_to_default_grouping_on_llm_failure():
    class _BoomLLM:
        async def complete(self, prompt, system):
            raise RuntimeError("model down")

    messages = [_msg("user", "Q"), _msg("assistant", "A")]
    pairs = asyncio.run(_segment_chat(messages, _BoomLLM()))
    assert pairs == [{"question": "Q", "answer": "A", "indices": [0]}]


def test_pending_chat_entries_skips_fully_covered_only():
    # Three entries; the first two are fully in the repo already → only the appended
    # third entry (new user message "m4") should still be written.
    pairs = [{"question": "Q1", "answer": "A1"}, {"question": "Q2", "answer": "A2"}, {"question": "Q3", "answer": "A3"}]
    covered_by_pair = [["m1"], ["m2", "m3"], ["m4"]]
    pending = _pending_chat_entries(pairs, covered_by_pair, existing={"m1", "m2", "m3"})
    assert [(i, p["question"], cov) for i, p, cov in pending] == [(2, "Q3", ["m4"])]


def test_pending_chat_entries_requires_an_anchor():
    # An entry with no covered user message can never be imported incrementally.
    pairs = [{"question": "Q", "answer": "A"}]
    assert _pending_chat_entries(pairs, [[]], existing=set()) == []


# ── text-extraction dispatch: .docx and .pdf ──
def test_extract_docx_paragraphs_and_table_cells():
    import io

    import docx as docx_lib

    buf = io.BytesIO()
    doc = docx_lib.Document()
    doc.add_paragraph("Hello paragraph")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "a"
    table.rows[0].cells[1].text = "b"
    doc.save(buf)

    text = extract_text(buf.getvalue(), "notes.docx")
    assert "Hello paragraph" in text
    assert "a | b" in text


def test_extract_document_text_delegates_plain_extract_for_txt():
    text = asyncio.run(extract_document_text("hello world".encode(), "notes.txt", llm=None))
    assert text == "hello world"


def test_extract_document_text_routes_pdf_to_pdf_extractor(monkeypatch):
    import core.infrastructure.pdf as pdf_mod

    calls = {}

    async def fake_extract(content, llm):
        calls["content"] = content
        calls["llm"] = llm
        return "pdf text"

    monkeypatch.setattr(pdf_mod, "extract_pdf_document", fake_extract)

    text = asyncio.run(extract_document_text(b"%PDF-1.4", "book.pdf", llm="LLM"))
    assert text == "pdf text"
    assert calls == {"content": b"%PDF-1.4", "llm": "LLM"}
