"""Tests for the golden-set regression harness (asset-level metrics)."""
from rag.eval import evaluate_case, load_golden, run_golden_set
from rag.eval import GoldenCase


def _hit(asset_id, chunk_id="c1"):
    return {"id": chunk_id, "text": "t", "score": 0.5, "meta": {"asset_id": asset_id}}


def test_recall_precision_mrr_computed():
    case = GoldenCase(id="g1", query="q", expect_asset_ids=["a", "b"])
    hits = [_hit("a"), _hit("x"), _hit("b")]
    r = evaluate_case(case, hits, top_k=5)
    assert r.recall_at_k == 1.0          # a and b both found
    assert r.precision_at_k == 0.4       # 2 of 5
    assert r.mrr == 1.0                  # first expected at rank 1


def test_mrr_reflects_rank():
    case = GoldenCase(id="g1", query="q", expect_asset_ids=["b"])
    hits = [_hit("x"), _hit("y"), _hit("b")]
    r = evaluate_case(case, hits, top_k=5)
    assert r.mrr == 0.3333
    assert r.recall_at_k == 1.0
    assert r.precision_at_k == 0.2


def test_missing_expected_scores_zero():
    case = GoldenCase(id="g1", query="q", expect_asset_ids=["z"])
    r = evaluate_case(case, [_hit("a")], top_k=5)
    assert r.recall_at_k == 0.0
    assert r.mrr == 0.0


def test_duplicate_asset_hits_count_once():
    case = GoldenCase(id="g1", query="q", expect_asset_ids=["a"])
    hits = [_hit("a", "c1"), _hit("a", "c2")]
    r = evaluate_case(case, hits, top_k=5)
    assert r.mrr == 1.0
    assert r.hit_asset_ids == ["a"]


def test_load_golden_skips_malformed(tmp_path):
    p = tmp_path / "golden.json"
    p.write_text(
        '[{"id": "ok", "query": "q", "expect_asset_ids": ["a"]},'
        '{"id": "no-query"},'
        '{"id": "empty-expect", "query": "x", "expect_asset_ids": []}]',
        encoding="utf-8",
    )
    cases = load_golden(p)
    assert [c.id for c in cases] == ["ok"]


def test_load_golden_missing_file_returns_empty(tmp_path):
    assert load_golden(tmp_path / "nope.json") == []


async def test_run_golden_set_uses_factory(tmp_path):
    p = tmp_path / "golden.json"
    p.write_text(
        '[{"id": "g1", "query": "q1", "expect_asset_ids": ["a"]},'
        '{"id": "g2", "query": "q2", "expect_asset_ids": ["zzz"]}]',
        encoding="utf-8",
    )
    calls = []

    def factory():
        calls.append(1)

        class _Pipe:
            async def retrieve(self, query, top_k=5, filters=None):
                return [_hit("a"), _hit("b")]

        return _Pipe()

    report = await run_golden_set(factory, str(p))
    assert len(calls) == 1  # one pipeline built, reused across all cases
    assert report.metrics["n_cases"] == 2
    by_id = {c.case_id: c for c in report.cases}
    assert by_id["g1"].recall_at_k == 1.0
    assert by_id["g2"].recall_at_k == 0.0
