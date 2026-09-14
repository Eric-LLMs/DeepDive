"""Smoke test for the content-to-slides deck engine: real LLM + real Typst → one 16:9 PDF.

BURNS REAL LLM TOKENS (one Pass A + one Pass B + N Pass C calls, plus corrective retries
if any). Runs the SAME lifecycle engine the worker job uses (``pipeline_for("slides", llm)``
→ validate → ingest → generate → render → persist), so the PDF it writes is the canonical
artifact an end user would get — this only skips the Redis/queue hop, not the engine.

Default source is a short embedded "RAG 工作流解析" document chosen for its process +
architecture + quantity shape (steps, components, real numbers) so Pass D has honest
visual material to pick from. Point ``--source`` at any workspace file to try your own.

The LLM channel resolves like the worker's default: the admin-configured credential from
the DB, falling back to the env gateway settings (``--no-db`` skips the DB lookup).

After the run, every artifact is copied under ``logs/smoke_slides/<UTC-stamp>/`` for
visual acceptance, and the PDF is verified in-process (page count, 16:9 aspect).

Usage (from the repo root, host venv):
    .venv/Scripts/python.exe scripts/smoke_test_slides.py --yes
    .venv/Scripts/python.exe scripts/smoke_test_slides.py --yes --count 6 \
        --source path/to/file.md --prompt "用中文"
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import settings
from core.infrastructure.llm import OpenAILLM

DEFAULT_TITLE = "RAG 工作流解析"

# A short source with deliberate step / architecture / quantity structure: Pass A must
# find facts, Pass B must sequence them, Pass D must have real numbers to allow a CHART.
DEFAULT_SOURCE = """# RAG 工作流解析

检索增强生成(RAG)把"先检索、再生成"作为默认路径:模型不再凭参数记忆作答,
而是先从知识库里取回证据,再让 LLM 基于证据写作。整条链路由离线索引与在线问答两个阶段组成。

## 离线索引阶段

1. 文档切块:按标题与段落边界把原始文档切成 800 字左右的 chunk,保留来源行号供引用定位。
2. 向量化:每个 chunk 经嵌入模型编码为 1024 维向量,写入向量库;同时把原文入倒排索引。
3. 入库校验:抽检 5% 的 chunk 验证嵌入服务返回维度一致,失败批次重放,索引覆盖率要求达到 99.5%。

## 在线问答阶段

1. 查询改写:把口语化问题改写为检索友好的表述,一次 LLM 调用完成。
2. 混合检索:向量检索召回 top-50,BM25 关键词检索召回 top-50,两路结果用 RRF 融合,
   常数 k 取 60,融合后截取 top-8 进入上下文。
3. 重排与生成:对 top-8 用交叉编码器重排,拼进 prompt 交给生成模型,输出带行号引用的回答。

## 关键设计权衡

- RRF 融合不依赖两路分数的可比性,只取名次,工程上比加权分数融合更稳,调参成本从 3 个超参降到 1 个。
- top-8 是延迟与召回率的折中:实测 top-4 时答案覆盖率 78%,top-8 提升到 91%,
  而平均端到端延迟从 210ms 涨到 320ms,仍在可接受区间。
- 缓存层:高频问题直接命中语义缓存,线上命中率约 35%,整体成本下降 0.565 USD 每千次查询。

## 架构组件

客户端 → API 网关 → 查询改写器 → 检索编排器(向量库 / 倒排索引)→ 重排器 → 生成模型 → 引用装配器。
全链路以异步任务队列削峰,索引与问答两条链路共享同一对象存储,但写入只发生在离线索引侧。
"""


async def build_llm(args: argparse.Namespace) -> tuple[OpenAILLM, str]:
    """Worker-style channel resolution: admin credential from DB, else env gateway."""
    base_url = api_key = model = None
    source = "env settings"
    if not args.no_db:
        try:
            from apps.worker.settings import _active_llm_channel

            base_url, api_key, model = await _active_llm_channel()
            if api_key:
                source = "admin DB channel"
            else:
                base_url = api_key = model = None
        except Exception as exc:  # noqa: BLE001 - a smoke tool must fall back, not die
            print(f"[smoke] DB channel unavailable ({exc.__class__.__name__}: {exc}); "
                  f"falling back to env settings", file=sys.stderr)
    llm = OpenAILLM(
        api_key=args.api_key or api_key,
        base_url=args.base_url or base_url,
        model=args.model or model,
    )
    return llm, f"{source} (model={llm.model}, base_url={llm.client.base_url})"


def _verify_pdf(pdf: Path) -> None:
    import pymupdf

    doc = pymupdf.open(pdf)
    try:
        n = doc.page_count
        w, h = (doc[0].rect.width, doc[0].rect.height)
    finally:
        doc.close()
    ratio = w / h
    print(f"[smoke] PDF check: {n} pages, {w:.0f}x{h:.0f} pt, aspect {ratio:.3f}")
    if abs(ratio - 16 / 9) > 0.02:
        raise SystemExit(f"[smoke] FAIL: aspect {ratio:.3f} is not 16:9")
    if n < 2:
        raise SystemExit(f"[smoke] FAIL: only {n} page(s)")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--yes", action="store_true", help="confirm spending real LLM tokens")
    ap.add_argument("--source", help="workspace file to deck-ify (default: embedded RAG doc)")
    ap.add_argument("--title", default=DEFAULT_TITLE, help="deck/source display title")
    ap.add_argument("--count", type=int, default=6, help="content slides (3..20)")
    ap.add_argument("--audience", default="工程团队内部技术评审")
    ap.add_argument("--goal", default="讲清 RAG 全链路与关键设计取舍")
    ap.add_argument("--prompt", default="", help="optional custom instruction (hint)")
    ap.add_argument("--no-db", action="store_true", help="skip the DB admin-channel lookup")
    ap.add_argument("--api-key")
    ap.add_argument("--base-url")
    ap.add_argument("--model")
    ap.add_argument("--out", default="logs/smoke_slides", help="copy destination root")
    args = ap.parse_args()

    if not args.yes and sys.stdin.isatty():
        if input("[smoke] This calls the REAL LLM. Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    if not args.yes and not sys.stdin.isatty():
        print("[smoke] non-interactive runs require --yes", file=sys.stderr)
        return 2
    if shutil.which("typst") is None:
        print("[smoke] FAIL: typst binary not on PATH", file=sys.stderr)
        return 2

    llm, channel = await build_llm(args)
    print(f"[smoke] LLM channel: {channel}")

    workspace = Path(settings.workspace_dir)
    src_path = Path(args.source) if args.source else workspace / "slides_smoke_src.md"
    if not args.source:
        src_path.write_text(DEFAULT_SOURCE, encoding="utf-8")
    print(f"[smoke] source: {src_path} ({src_path.stat().st_size} bytes)")

    from apps.api.tools.toolkit import pipeline_for

    pipeline = pipeline_for("slides", llm)
    t0 = time.perf_counter()
    result = await pipeline.run(
        [str(src_path)],
        count=args.count, audience=args.audience, goal=args.goal, prompt=args.prompt,
    )
    elapsed = time.perf_counter() - t0
    print(f"[smoke] pipeline OK in {elapsed:.1f}s — {result.summary}")

    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    out_dir = Path(args.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf: Path | None = None
    for f in result.files:
        p = Path(f)
        dest = out_dir / p.name
        shutil.copy2(p, dest)
        print(f"[smoke]   {p.suffix:>6} → {dest}")
        if p.suffix == ".pdf":
            pdf = dest
    if pdf is None:
        print("[smoke] FAIL: no deck.pdf in the result", file=sys.stderr)
        return 1
    _verify_pdf(pdf)
    print(f"[smoke] visual acceptance: open {pdf.resolve()}")
    if not args.source:
        src_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
