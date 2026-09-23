#!/usr/bin/env python3
"""
Probe how PageIndex builds a tree from a Markdown file.

Reports the tree shape, whether tables survive into node text, and what the
node addressing unit looks like. Use it to judge whether a corpus of
company-authored Markdown will index well before committing to it.

Usage:
    python scripts/03_md_tree_probe.py samples/aia_ar2021_excerpt.md
    python scripts/03_md_tree_probe.py path/to/report.md --no-summary
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")


def walk(nodes, depth=1):
    for n in nodes or []:
        yield n, depth
        yield from walk(n.get("nodes"), depth + 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("md_path")
    ap.add_argument("--no-summary", action="store_true",
                    help="skip LLM summaries (structure only, free)")
    ap.add_argument("--keep-text", action="store_true",
                    help="keep node text in the tree")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    md = Path(args.md_path)
    if not md.is_file():
        print(f"not found: {md}", file=sys.stderr)
        return 1

    from pageindex.page_index_md import md_to_tree

    tree = asyncio.run(md_to_tree(
        md_path=str(md),
        if_add_node_summary="no" if args.no_summary else "yes",
        if_add_node_text="yes" if args.keep_text else "no",
        if_add_node_id="yes",
        # md_to_tree() defaults this to None and get_node_summary() then does
        # `num_tokens < summary_token_threshold`, which raises TypeError.
        # The CLI's own default is 200; pass it explicitly.
        summary_token_threshold=200,
        model="deepseek/deepseek-flash",
        summary_model="deepseek/deepseek-flash",
    ))

    flat = list(walk(tree.get("structure")))
    lines = md.read_text(encoding="utf-8").split("\n")

    print("=" * 74)
    print(f"文件      : {md}")
    print(f"总行数    : {tree.get('line_count')}")
    print(f"节点数    : {len(flat)}")
    print(f"树深      : {max((d for _, d in flat), default=0)}")
    print(f"寻址字段  : {sorted({k for n, _ in flat for k in n})}")
    print("=" * 74)
    print()

    print("树结构（缩进表示层级，[行号] 是寻址单位）:")
    for n, d in flat:
        print(f"{'  ' * (d - 1)}- [{n.get('line_num')}] {n.get('title')}")
    print()

    # 节点范围是怎么推出来的：下一个标题的行号
    print("节点覆盖范围（由相邻标题的行号推出，不是显式区间）:")
    seq = [n for n, _ in flat]
    for i, n in enumerate(seq):
        start = n.get("line_num")
        end = seq[i + 1].get("line_num", tree.get("line_count")) if i + 1 < len(seq) else tree.get("line_count")
        print(f"  [{n.get('node_id')}] 行 {start}–{end}  {n.get('title')}")
    print()

    # 表格是否幸存
    n_tables = sum(1 for ln in lines if ln.strip().startswith("|"))
    print(f"源文件里的 markdown 表格行数: {n_tables}")
    if args.keep_text:
        blob = json.dumps(tree, ensure_ascii=False)
        print(f"树里保留的表格行数          : {blob.count(chr(124))}")
        print("-> 表格作为普通文本留在节点 text 里，没有变成结构化字段")
    else:
        print("-> 默认 if_add_node_text='no'，正文（含表格）在生成摘要后被剥掉")
    print()

    if args.out:
        Path(args.out).write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"完整树已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
