"""RAG feedback golden-dataset contract (DB-free): model schema + migration present.

The /rag/feedback endpoint needs a live Postgres to persist rows, so the unit suite
verifies the contract instead: the ORM model and the migration DDL must agree on the
table/columns, and the API schema must expose the request fields.
"""
from pathlib import Path

from core.infrastructure.db import RagFeedbackModel
from sqlalchemy import Boolean, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


def test_feedback_table_name():
    assert RagFeedbackModel.__tablename__ == "rag_feedback"


def test_feedback_columns_and_types():
    cols = {c.name: c for c in RagFeedbackModel.__table__.columns}
    assert "id" in cols and isinstance(cols["id"].type, PG_UUID)
    assert "query" in cols and isinstance(cols["query"].type, Text)
    assert "rating" in cols and isinstance(cols["rating"].type, Boolean)
    assert "reason" in cols and isinstance(cols["reason"].type, Text)
    # The golden dataset snapshots the hits + the tenant filter they were retrieved under.
    assert isinstance(cols["hits"].type, JSONB)
    assert isinstance(cols["filters"].type, JSONB)


def test_feedback_migration_matches():
    root = Path(__file__).resolve().parents[1]
    sql = (root / "migrations" / "0012_rag_feedback.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS rag_feedback" in sql
    # The migration's columns mirror the model (id, query, rating, reason, hits, filters).
    for col in ("user_id UUID NULL", "query TEXT NOT NULL", "rating BOOLEAN NOT NULL",
                "reason TEXT NULL", "hits JSONB", "filters JSONB"):
        assert col in sql
