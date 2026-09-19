"""Text extraction + chunking for the asset RAG-ingestion pipeline."""
import asyncio
import io
import subprocess

import pytest
from core.infrastructure.ingest import (
    EXCEL_MAX_ROWS,
    EXCEL_MAX_SHEETS,
    UnsupportedFileType,
    _strip_control_chars,
    extract_document_text,
    extract_text,
    split_chunks,
    supported_extensions,
)


def test_extract_text_plain_text_decoded():
    assert extract_text("hello 世界".encode(), "notes.md") == "hello 世界"


def test_strip_control_chars_drops_nul_keeps_whitespace():
    assert _strip_control_chars("a\x00b\x01c") == "abc"
    assert _strip_control_chars("keep\t\n\r\x00drop") == "keep\t\n\rdrop"
    assert _strip_control_chars("\x7f\x00") == ""


def test_extract_document_text_strips_nul_from_plain_text():
    text = asyncio.run(extract_document_text(b"eq \x00s ln x + C\nnext line", "notes.md", llm=None))
    assert text == "eq s ln x + C\nnext line"


def test_extract_text_subtitle_joins_cue_texts():
    srt = (
        b"1\n00:00:01,000 --> 00:00:03,000\nFirst line\n\n"
        b"2\n00:00:04,000 --> 00:00:06,000\nSecond line\n"
    )
    assert extract_text(srt, "clip.srt") == "First line\nSecond line"


def test_extract_text_unsupported_extension_raises():
    with pytest.raises(UnsupportedFileType):
        extract_text(b"%PDF-1.4", "book.pdf")


def test_split_chunks_short_text_single_chunk():
    assert split_chunks("a few words", chunk_chars=1000, overlap=100) == ["a few words"]


def test_split_chunks_empty_text():
    assert split_chunks("   \n\t ", chunk_chars=100, overlap=20) == []


def test_split_chunks_long_text_overlaps_and_bounds():
    text = "word " * 500  # ~2500 chars
    chunks = split_chunks(text, chunk_chars=300, overlap=30)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)
    # Consecutive chunks share the overlap tail.
    assert text.find(chunks[1]) < text.find(chunks[0]) + 300


# ── Excel extraction (research materials path) ───────────────────────────────

def _xlsx_bytes(rows_by_sheet: dict[str, list[list]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    first = True
    for title, rows in rows_by_sheet.items():
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = title
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_extract_excel_grid_and_sheet_headers():
    content = _xlsx_bytes({
        "Sales": [["region", "amount"], ["east", 10], [], ["west", 20]],
        "Notes": [["memo"], ["ok"]],
    })
    text = extract_text(content, "book.xlsx")
    assert "## sheet: Sales" in text
    assert "## sheet: Notes" in text
    assert "region\tamount" in text
    assert "east\t10" in text
    # Empty rows are skipped, never emitted as blank lines.
    assert "\n\n" not in text.replace("## sheet:", "")


def test_extract_excel_row_cap_marks_truncation():
    content = _xlsx_bytes({"Big": [[i] for i in range(EXCEL_MAX_ROWS + 5)]})
    text = extract_text(content, "big.xlsx")
    assert "[rows truncated]" in text
    assert text.count("\n") - text.count("## sheet") <= EXCEL_MAX_ROWS + 3


def test_extract_excel_sheet_cap_marks_truncation():
    many = {f"S{i}": [["x"]] for i in range(EXCEL_MAX_SHEETS + 2)}
    content = _xlsx_bytes(many)
    text = extract_text(content, "many.xlsx")
    assert "sheets truncated]" in text
    assert f"## sheet: S{EXCEL_MAX_SHEETS}" not in text


def test_extract_text_xls_legacy_raises_clear_message():
    with pytest.raises(UnsupportedFileType, match="resave as .xlsx"):
        extract_text(b"\xd0\xcf\x11\xe0anything", "old.xls")


def test_supported_extensions_includes_excel():
    exts = supported_extensions()
    assert {".xlsx", ".xlsm"} <= exts
    assert ".xls" not in exts


def test_supported_extensions_includes_legacy_doc():
    assert ".doc" in supported_extensions()


def test_extract_doc_runs_antiword_with_utf8_mapping(monkeypatch):
    from core.infrastructure import ingest

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="内容 hello".encode(), stderr=b"")

    monkeypatch.delenv("ANTIWORD_PATH", raising=False)
    monkeypatch.setattr(ingest.shutil, "which", lambda name: "/usr/bin/antiword")
    monkeypatch.setattr(ingest.subprocess, "run", fake_run)
    text = extract_text(b"\xd0\xcf\x11\xe0junk", "old.doc")
    assert text == "内容 hello"
    assert seen["cmd"][0] == "/usr/bin/antiword"
    assert "-m" in seen["cmd"] and "UTF-8.txt" in seen["cmd"]
    assert seen["cmd"][-1].endswith(".doc")


def test_extract_doc_without_antiword_raises_clear_message(monkeypatch):
    from core.infrastructure import ingest

    monkeypatch.delenv("ANTIWORD_PATH", raising=False)
    monkeypatch.setattr(ingest.shutil, "which", lambda name: None)
    with pytest.raises(UnsupportedFileType, match="resave as .docx"):
        extract_text(b"\xd0\xcf\x11\xe0junk", "old.doc")


def test_extract_doc_antiword_failure_raises_clear_message(monkeypatch):
    from core.infrastructure import ingest

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"not a Word document")

    monkeypatch.delenv("ANTIWORD_PATH", raising=False)
    monkeypatch.setattr(ingest.shutil, "which", lambda name: "/usr/bin/antiword")
    monkeypatch.setattr(ingest.subprocess, "run", fake_run)
    with pytest.raises(UnsupportedFileType, match="resave as .docx"):
        extract_text(b"\xd0\xcf\x11\xe0junk", "old.doc")


def test_extract_doc_dispatched_before_generic_decode(monkeypatch):
    # These bytes are latin-1 decodable — if the .doc branch were missing, the generic
    # decode fallthrough would happily return mojibake instead of refusing.
    from core.infrastructure import ingest

    monkeypatch.delenv("ANTIWORD_PATH", raising=False)
    monkeypatch.setattr(ingest.shutil, "which", lambda name: None)
    with pytest.raises(UnsupportedFileType):
        ingest.extract_text(b"\xd0\xcf\x11\xe0data", "old.doc")

