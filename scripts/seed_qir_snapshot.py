"""Seed/refresh the QIR capability snapshot (Batch 1 operational entry point).

Usage:
    python scripts/seed_qir_snapshot.py            # validate + publish DRAFT
    python scripts/seed_qir_snapshot.py --check    # validate + print version, no write
    python scripts/seed_qir_snapshot.py --unpublish  # emergency stop (routing abstains)

The draft is the ROUTING projection of the existing ``DIRECT_TOOLS`` allowlist:
every ``tool_binding`` here must already exist in ``DIRECT_TOOLS`` — publication
rejects anything else (QIR cannot invent executables). Examples are platform-
global phrasings (no user content). Embeddings come from the same TEIEmbedder
the RAG pipeline uses; any embedding failure raises BEFORE a transaction opens,
so a bad draft can never half-publish.
"""
from __future__ import annotations

import argparse
import asyncio
import json

from core.application.chat.actions import DIRECT_TOOLS
from core.application.chat.qir import store
from core.application.chat.qir.snapshot import validate_draft
from core.infrastructure.db import SessionLocal
from core.infrastructure.vector import TEIEmbedder

# tool_binding -> routing examples. Phrasings here only need to be SEMANTICALLY
# close; the exact regex matchers in DIRECT_TOOLS still do the L0 work — QIR is
# the synonym/ambiguity layer on top, and argument binding reuses the same
# extractors via bind_arguments().
DRAFT: dict = {
    "capabilities": [
        {
            "id": "cap-create-folder",
            "tool_binding": "create_folder",
            "description": "Create a new folder with an explicit name.",
            "examples": (
                'create a folder named "reports"',
                'make a new directory called "archive"',
                "新建文件夹「报表」",
                "创建一个叫“资料”的文件夹",
                "帮我新建一个目录，名字是“Q3”",
            ),
            "negatives": (
                "不要新建文件夹",
                "列出所有文件夹",
                "删除文件夹",
            ),
        },
        {
            "id": "cap-add-term",
            "tool_binding": "add_term",
            "description": "Add a term to an explicitly-named vocabulary domain.",
            "examples": (
                'add "quark" to my physics vocab',
                'put "entropy" into the science word list',
                "把“熵”加入我的科学词汇库",
                '将 "gradient" 添加到机器学习词汇表',
            ),
            "negatives": (
                "查询 quark 的释义",
                "我的物理词汇库里有哪些词",
                "从词汇库删除 entropy",
            ),
        },
        {
            "id": "cap-pdf-extract-text",
            "tool_binding": "pdf_extract_text",
            "description": "Extract the full text of the attached document.",
            "examples": (
                "extract the full text of this pdf",
                "get all the text from the attached document",
                "提取这个文件的全文",
                "把附件里的文字全部取出来",
            ),
            "negatives": (
                "不需要提取全文",
                "总结这份文档",
                "这份文档讲了什么",
            ),
        },
    ]
}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="validate + embed, no publish")
    ap.add_argument("--unpublish", action="store_true", help="remove the active snapshot")
    args = ap.parse_args()

    if args.unpublish:
        await store.unpublish(SessionLocal)
        print("QIR snapshot unpublished — routing now abstains everywhere.")
        return 0

    # Fail fast on registry drift before paying the embedding cost.
    for cap in DRAFT["capabilities"]:
        assert cap["tool_binding"] in DIRECT_TOOLS, cap["tool_binding"]
    validate_draft(DRAFT)  # structural + allowlist gate, same as publish

    embedder = TEIEmbedder()
    if args.check:
        from core.application.chat.qir.snapshot import build_snapshot

        snap = await build_snapshot(DRAFT, embedder)
        print(json.dumps(
            {"version": snap.version, "capabilities": [c.id for c in snap.capabilities],
             "vectors": len(snap.example_vectors)},
            ensure_ascii=False, indent=2,
        ))
        print("(--check: nothing written)")
        return 0

    snap = await store.publish(DRAFT, embedder, SessionLocal)
    print(f"published QIR snapshot version={snap.version} "
          f"capabilities={len(snap.capabilities)} vectors={len(snap.example_vectors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
