#!/usr/bin/env python3
"""
Stage 0 — extract PDFs to Markdown with Azure AI Document Intelligence.

    # validate the .env config and analyse just page 1 of one PDF (cheap smoke test)
    python scripts/06_azure_extract.py data/aia_reports --check

    # convert everything
    python scripts/06_azure_extract.py data/aia_reports --out corpus_md

    # convert one file, first 5 pages only
    python scripts/06_azure_extract.py data/aia_reports --out corpus_md \
        --only FY2021 --pages 1-5 --force

    # then build the navigation index over the Markdown
    python -m nav.build corpus_md --out corpus_index --summarize-files

Why bother: the built-in PageIndex local path reads the PDF text layer with
PyPDF2. On financial reports that mangles tables (a chart page comes out as
`175230`, two numbers fused) and refuses scanned PDFs. Azure's layout model
returns real Markdown tables and OCRs image-only pages, and we inject
`<!-- page: N -->` markers so page-level citations survive.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from extractors.azure_di import (  # noqa: E402
    AzureDIConfig, AzureDIError, AzureDocIntelligence, extract_corpus,
)


def cmd_check(cfg: AzureDIConfig, sample: Path | None) -> int:
    """Validate config, then optionally analyse one page of one PDF."""
    print("Azure Document Intelligence 配置检查")
    print("=" * 74)
    key_disp = (f"{'*' * 8}{cfg.key[-4:]} (长度 {len(cfg.key)})"
                if cfg.key else "(未设置)")
    print(f"  endpoint      : {cfg.endpoint or '(未设置)'}")
    print(f"  key           : {key_disp}")
    print(f"  api-version   : {cfg.api_version}")
    print(f"  model         : {cfg.model}")
    print(f"  output format : {cfg.output_format}")
    print(f"  string index  : {cfg.string_index_type}")
    print(f"  features      : {', '.join(cfg.features) or '(none)'}")
    print(f"  locale        : {cfg.locale or '(auto)'}")
    print()
    try:
        cfg.validate()
    except AzureDIError as exc:
        print(f"❌ 配置有问题:\n{exc}")
        return 1
    print("✅ 配置格式正确")

    if sample is None:
        print("\n（未指定 --only，跳过真实调用。加 --only <文件名片段> 可做一次 1 页试跑）")
        return 0

    print(f"\n试跑：只分析 {sample.name} 的第 1 页 ...")
    try:
        adi = AzureDocIntelligence(cfg)
        md, result = adi.extract(sample, pages="1")
    except AzureDIError as exc:
        print(f"❌ 调用失败:\n{exc}")
        return 1
    info = AzureDocIntelligence.describe(result)
    print(f"✅ 成功 — 页数 {info['pages']}，表格 {info['tables']}，"
          f"段落 {info['paragraphs']}，正文 {info['content_chars']} 字符")
    print(f"   页标记注入: {'是' if '<!-- page:' in md else '否'}")
    print()
    print("--- 返回的 Markdown 前 700 字符 ---")
    print(md[:700])
    return 0


def cmd_extract(cfg: AzureDIConfig, args) -> int:
    src = Path(args.src)
    pdfs = sorted(p for p in src.rglob("*.pdf")) if src.is_dir() else [src]
    if args.only:
        pdfs = [p for p in pdfs if args.only.lower() in p.name.lower()]
    if not pdfs:
        print(f"没找到匹配的 PDF（--only {args.only!r}）", file=sys.stderr)
        return 1

    print(f"源目录  : {src}")
    print(f"输出目录: {args.out}")
    print(f"待处理  : {len(pdfs)} 个 PDF"
          + (f"（只分析第 {args.pages} 页）" if args.pages else ""))
    print(f"并发    : {args.workers}")
    print()

    def progress(i, total, pdf, status, detail):
        mark = {"converted": "✅", "skipped": "⏭️", "failed": "❌"}.get(status, "?")
        extra = ""
        if status == "converted" and hasattr(detail, "name"):
            extra = f" -> {Path(detail).name}"
        elif status == "failed":
            extra = f"  {detail}"
        print(f"  [{i}/{total}] {mark} {pdf.name}{extra}", flush=True)

    try:
        summary = extract_corpus(
            src, args.out,
            force=args.force, workers=args.workers, pages=args.pages,
            page_markers=not args.no_page_markers,
            client=AzureDocIntelligence(cfg),
            on_progress=progress,
        )
    except AzureDIError as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        return 1

    print()
    print("=" * 74)
    print(f"  总计     : {summary['total']}")
    print(f"  已转换   : {summary['converted']}")
    print(f"  跳过     : {summary['skipped']}")
    print(f"  失败     : {summary['failed']}")
    print(f"  页数合计 : {summary['pages']}")
    print(f"  表格合计 : {summary['tables']}")
    if summary["errors"]:
        print("\n  失败明细:")
        for e in summary["errors"][:10]:
            print(f"    - {e}")
    print()
    print(f"输出目录: {Path(args.out).resolve()}")
    print("下一步:")
    print(f"  python -m nav.build {args.out} --out corpus_index --summarize-files")
    return 1 if summary["failed"] and not summary["converted"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="用 Azure AI Document Intelligence 把 PDF 转成 Markdown")
    ap.add_argument("src", help="PDF 文件或目录")
    ap.add_argument("--out", default="corpus_md", help="Markdown 输出目录")
    ap.add_argument("--check", action="store_true",
                    help="只校验配置；配合 --only 可做 1 页试跑")
    ap.add_argument("--only", default=None, help="只处理文件名含该片段的 PDF")
    ap.add_argument("--pages", default=None,
                    help='只分析指定页，如 "1-5" 或 "1,3,7"（省钱试跑用）')
    ap.add_argument("--workers", type=int, default=2,
                    help="并发数，默认 2；免费版 F0 限流很严，别调高")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 .md")
    ap.add_argument("--no-page-markers", action="store_true",
                    help="不注入 <!-- page: N --> 标记")
    args = ap.parse_args()

    cfg = AzureDIConfig.from_env()

    if args.check:
        sample = None
        if args.only:
            src = Path(args.src)
            cands = (sorted(p for p in src.rglob("*.pdf")
                            if args.only.lower() in p.name.lower())
                     if src.is_dir() else [src])
            if not cands:
                print(f"没找到匹配 {args.only!r} 的 PDF", file=sys.stderr)
                return 1
            sample = cands[0]
        return cmd_check(cfg, sample)

    return cmd_extract(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
