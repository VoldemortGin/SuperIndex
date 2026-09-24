#!/usr/bin/env python3
"""
Quantify cross-document duplication in a corpus, and what it costs retrieval.

Financial reports repeat themselves: definition notes, disclaimers, basis-of-
preparation boilerplate and even whole paragraphs reappear near-verbatim across
years. Vector search cannot tell those copies apart — and worse, they consume
top-k slots that should have gone to the passage containing the actual numbers.

This script measures three things:
  1. how much of the corpus is near-duplicate ACROSS documents
  2. for a real query, the score gap between the right answer and its duplicate
  3. how many top-k slots get wasted on duplicate text

Usage:
    uv run python scripts/05_similarity_probe.py data/aia_reports --pages 5
    uv run python scripts/05_similarity_probe.py data/aia_reports --queries queries.txt

Needs fastembed, which is not a project dependency: add `--with fastembed` after `uv run`.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_QUERIES = [
    "What were the VONB and OPAT figures in 2024?",
    "What were the VONB and OPAT figures in 2023?",
    "VONB and OPAT definitions",
]


def read_pages(path: Path) -> list[str]:
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(path))
        try:
            return [re.sub(r"\s+", " ", doc[i].get_textpage().get_text_range()).strip()
                    for i in range(len(doc))]
        finally:
            doc.close()
    text = path.read_text(encoding="utf-8", errors="replace")
    return [re.sub(r"\s+", " ", blk).strip()
            for blk in re.split(r"\n\s*\n", text) if blk.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory")
    ap.add_argument("--pattern", default="*.pdf")
    ap.add_argument("--min-chars", type=int, default=200,
                    help="ignore chunks shorter than this")
    ap.add_argument("--dup-threshold", type=float, default=0.97,
                    help="cosine similarity at or above which two chunks are 'duplicates'")
    ap.add_argument("--queries", default=None, help="file with one query per line")
    ap.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args()

    import numpy as np
    from fastembed import TextEmbedding

    files = sorted(Path(args.directory).glob(args.pattern))
    if not files:
        print(f"no files matching {args.pattern} in {args.directory}", file=sys.stderr)
        return 1

    chunks, meta = [], []
    for f in files:
        for i, txt in enumerate(read_pages(f), start=1):
            if len(txt) >= args.min_chars:
                chunks.append(txt)
                meta.append((f.name, i))
    if not chunks:
        print("no chunks long enough to compare", file=sys.stderr)
        return 1

    print(f"语料: {len(files)} 个文件, {len(chunks)} 个 chunk (>= {args.min_chars} 字符)")
    print("正在计算 embedding ...")
    model = TextEmbedding()
    vecs = np.array(list(model.embed(chunks)), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)

    # ---- 1. cross-document duplication -----------------------------------
    dup_pairs = []
    for a, b in combinations(range(len(chunks)), 2):
        if meta[a][0] == meta[b][0]:
            continue                      # 只看跨文档重复
        s = float(vecs[a] @ vecs[b])
        if s >= args.dup_threshold:
            dup_pairs.append((s, a, b))

    involved = {i for _, a, b in dup_pairs for i in (a, b)}
    print()
    print("=" * 96)
    print("① 跨文档重复情况")
    print("=" * 96)
    print(f"  相似度 >= {args.dup_threshold} 的跨文档 chunk 对: {len(dup_pairs)}")
    print(f"  涉及 chunk 数: {len(involved)} / {len(chunks)} "
          f"({len(involved)/len(chunks)*100:.1f}%)")
    by_doc = defaultdict(int)
    for _, a, b in dup_pairs:
        by_doc[meta[a][0]] += 1
    if by_doc:
        print("\n  按文档统计（参与重复的次数）:")
        for name, n in sorted(by_doc.items(), key=lambda x: -x[1]):
            print(f"    {n:>4}  {name}")
    print("\n  最相似的 10 对:")
    for s, a, b in sorted(dup_pairs, reverse=True)[:10]:
        print(f"    {s:.6f}  {meta[a][0]} p{meta[a][1]}  <->  {meta[b][0]} p{meta[b][1]}")
        print(f"            {chunks[a][:88]}...")

    # ---- 2/3. retrieval discrimination -----------------------------------
    qfile = Path(args.queries) if args.queries else None
    queries = ([ln.strip() for ln in qfile.read_text(encoding="utf-8").splitlines() if ln.strip()]
               if qfile else DEFAULT_QUERIES)

    print()
    print("=" * 96)
    print("② 检索区分度（同一主题跨年份，模型能否把正确答案排上来）")
    print("=" * 96)
    for q in queries:
        qv = np.array(list(model.embed([q])), dtype=np.float32)[0]
        qv /= np.linalg.norm(qv)
        scores = vecs @ qv
        order = np.argsort(-scores)[: args.top_k]
        print(f"\n  问题: {q}")
        for r, i in enumerate(order, 1):
            print(f"    #{r}  {scores[i]:.4f}  {meta[i][0]} p{meta[i][1]}  | {chunks[i][:66]}...")
        if len(order) >= 2:
            gap = scores[order[0]] - scores[order[1]]
            print(f"    榜首-榜二差距: {gap:.6f}")
        # 有多少 top-k 槽位被重复文本占用
        seen, wasted = set(), 0
        for i in order:
            key = None
            for j in order:
                if j != i and float(vecs[i] @ vecs[j]) >= args.dup_threshold:
                    key = min(i, j)
            if key is not None:
                if key in seen:
                    wasted += 1
                seen.add(key)
        if wasted:
            print(f"    ⚠️ {wasted}/{len(order)} 个 top-k 槽位被重复文本占用（零新增信息）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
