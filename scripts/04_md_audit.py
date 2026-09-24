#!/usr/bin/env python3
"""
Audit a corpus of Markdown before indexing it with PageIndex.

PageIndex's Markdown path builds the tree purely from heading syntax, so the
tree it will produce is fully predictable from the file itself — no LLM needed.
This script simulates that extraction exactly and reports, per file:

  * the tree you would actually get (node count, depth)
  * which heading conventions the file mixes
  * page / slide markers (useful anchors, or noise to strip)
  * boilerplate lines repeated across the corpus

and routes each file to a processing path.

Usage:
    uv run python scripts/04_md_audit.py samples/
    uv run python scripts/04_md_audit.py samples/ --json
    uv run python scripts/04_md_audit.py a.md b.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# --- the exact patterns PageIndex uses (page_index_md.py) ------------------
HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$")
BOLD_HEADING_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
CODE_FENCE_RE = re.compile(r"^```")

# --- extra signals this auditor looks for ----------------------------------
NUMBERED_HEADING_RES = [
    re.compile(r"^\d+(\.\d+)*[\.、)]?\s+\S"),
    re.compile(r"^第[一二三四五六七八九十百零〇\d]+[章节節部编篇部分]"),
    re.compile(r"^(Part|Chapter|Section|Appendix|Annex)\s+[\dIVXivx]+", re.I),
]
PAGE_MARKER_RES = [
    re.compile(r"<!--\s*page[:\s]", re.I),
    re.compile(r"^\s*(?:Page|P\.?)\s*\d+\s*$", re.I),
    re.compile(r"^\s*第\s*\d+\s*页\s*$"),
    re.compile(r"^\s*Slide\s*\d+\s*$", re.I),
    re.compile(r"^\s*\d+\s*/\s*\d+\s*$"),
    re.compile(r"\f"),
    re.compile(r"^\s*---+\s*$"),
]
BOILERPLATE_HINT_RE = re.compile(
    r"(免责声明|disclaimer|confidential|版权所有|all rights reserved|"
    r"本公告|仅供|内部资料|do not distribute)", re.I)


def extract_headings(lines: list[str]):
    """Replicate PageIndex's extract_nodes_from_markdown, including its quirk
    that a line of only **bold** becomes a LEVEL-1 heading."""
    nodes = []
    in_code = False
    for i, raw in enumerate(lines, start=1):
        s = raw.strip()
        if CODE_FENCE_RE.match(s):
            in_code = not in_code
            continue
        if not s:
            continue
        if in_code:
            continue
        m = HEADER_RE.match(s)
        if m:
            nodes.append({"title": m.group(2).strip(), "line": i,
                          "level": len(m.group(1)), "kind": "atx"})
            continue
        b = BOLD_HEADING_RE.match(s)
        if b and b.group(1).strip():
            nodes.append({"title": b.group(1).strip(), "line": i,
                          "level": 1, "kind": "bold"})
    return nodes


def predict_tree(nodes: list[dict]) -> tuple[int, int]:
    """Run PageIndex's stack algorithm to get (node_count, max_depth)."""
    depth, max_depth, count = 0, 0, 0
    stack = []
    for n in nodes:
        count += 1
        lvl = n["level"]
        while stack and stack[-1] >= lvl:
            stack.pop()
        stack.append(lvl)
        depth = len(stack)
        max_depth = max(max_depth, depth)
    return count, max_depth


def audit_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    nodes = extract_headings(lines)
    n_nodes, depth = predict_tree(nodes)

    kinds = Counter(n["kind"] for n in nodes)
    levels = Counter(n["level"] for n in nodes)

    page_markers = sum(1 for ln in lines for rx in PAGE_MARKER_RES if rx.search(ln))
    slide_markers = sum(1 for ln in lines
                        if re.match(r"^\s*Slide\s*\d+\s*$", ln, re.I))
    numbered = sum(1 for ln in lines if any(rx.match(ln.strip()) for rx in NUMBERED_HEADING_RES))
    tables = sum(1 for ln in lines if ln.strip().startswith("|"))
    bullets = sum(1 for ln in lines if re.match(r"^\s*[-*+]\s+\S", ln))
    boiler = sum(1 for ln in lines if BOILERPLATE_HINT_RE.search(ln))

    first_heading_line = nodes[0]["line"] if nodes else None
    orphan = (first_heading_line - 1) if first_heading_line else len(lines)
    chars = len(text)
    approx_tokens = chars // 3  # CJK-heavy text; rough but adequate for ratios
    density = (len(nodes) / (approx_tokens / 1000)) if approx_tokens else 0

    # ---- verdict -----------------------------------------------------------
    if not nodes:
        verdict, advice = "empty", "无任何标题 → 树为空。必须规范化，或改走「分块 + 摘要」路径"
    elif depth <= 1:
        verdict, advice = "flat", "树只有一层 → 无法层级剪枝。建议规范化标题层级"
    elif density < 0.5:
        verdict, advice = "sparse", "标题过于稀疏 → 单节点过大。建议补细分标题"
    else:
        verdict, advice = "ok", "结构可用 → 可直接 md_to_tree"

    if kinds.get("bold") and not kinds.get("atx"):
        advice += "；全篇用 **加粗** 当标题（PageIndex 一律当一级），疑似 PPT 转出"
    elif kinds.get("bold") and kinds.get("atx"):
        advice += "；**加粗** 与 # 标题混用，加粗行会变成一级 → 层级会被打乱"
    if slide_markers:
        advice += "；检测到 Slide 标记，建议按 slide 切分"

    return {
        "file": path.name,
        "chars": chars,
        "lines": len(lines),
        "approx_tokens": approx_tokens,
        "headings_total": len(nodes),
        "headings_atx": kinds.get("atx", 0),
        "headings_bold": kinds.get("bold", 0),
        "max_level": max(levels) if levels else 0,
        "predicted_nodes": n_nodes,
        "predicted_depth": depth,
        "heading_density_per_1k": round(density, 2),
        "numbered_pseudo": numbered,
        "page_markers": page_markers,
        "slide_markers": slide_markers,
        "tables": tables,
        "bullets": bullets,
        "boilerplate_lines": boiler,
        "orphan_lines": orphan,
        "verdict": verdict,
        "advice": advice,
    }


def corpus_boilerplate(reports: list[dict], files: list[Path]) -> list[tuple[str, int]]:
    """Lines appearing in a majority of files are almost certainly headers,
    footers or disclaimers — they pollute retrieval and should be stripped."""
    counts = Counter()
    for p in files:
        seen = set()
        for ln in p.read_text(encoding="utf-8", errors="replace").split("\n"):
            s = ln.strip()
            if 8 <= len(s) <= 200:
                seen.add(s)
        for s in seen:
            counts[s] += 1
    threshold = max(2, len(files) // 2 + 1)
    return [(s, c) for s, c in counts.most_common(12) if c >= threshold]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    files: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.md")))
        elif p.is_file():
            files.append(p)
    if not files:
        print("no markdown files found", file=sys.stderr)
        return 1

    reports = [audit_file(p) for p in files]

    if args.json:
        print(json.dumps({"files": reports,
                          "boilerplate": corpus_boilerplate(reports, files)},
                         ensure_ascii=False, indent=2))
        return 0

    print("=" * 100)
    print(f"Markdown 结构审计 —— {len(files)} 个文件")
    print("=" * 100)
    hdr = (f"{'文件':<30}{'tokens':>8}{'标题':>6}{'#':>5}{'**':>5}"
           f"{'预测节点':>9}{'深度':>6}{'页标':>6}{'表格':>6}{'判定':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in reports:
        print(f"{r['file'][:29]:<30}{r['approx_tokens']:>8}{r['headings_total']:>6}"
              f"{r['headings_atx']:>5}{r['headings_bold']:>5}"
              f"{r['predicted_nodes']:>9}{r['predicted_depth']:>6}"
              f"{r['page_markers']:>6}{r['tables']:>6}{r['verdict']:>8}")

    print()
    print("=" * 100)
    print("逐文件建议")
    print("=" * 100)
    for r in reports:
        flag = {"ok": "✅", "sparse": "⚠️", "flat": "⚠️", "empty": "❌"}[r["verdict"]]
        print(f"\n{flag} {r['file']}  ({r['verdict']}, 深度 {r['predicted_depth']})")
        print(f"   {r['advice']}")
        extras = []
        if r["numbered_pseudo"]:
            extras.append(f"编号式伪标题 {r['numbered_pseudo']}")
        if r["orphan_lines"] > 5:
            extras.append(f"首个标题前有 {r['orphan_lines']} 行游离文本")
        if r["boilerplate_lines"]:
            extras.append(f"疑似样板行 {r['boilerplate_lines']}")
        if extras:
            print(f"   其他: {'；'.join(extras)}")

    bp = corpus_boilerplate(reports, files)
    if bp:
        print()
        print("=" * 100)
        print(f"跨文件重复行（出现在过半文件中 → 建议索引前剥离）")
        print("=" * 100)
        for s, c in bp:
            print(f"  [{c}/{len(files)}] {s[:88]}")

    dist = Counter(r["verdict"] for r in reports)
    print()
    print("=" * 100)
    print("汇总: " + "  ".join(f"{k}={v}" for k, v in dist.most_common()))
    ok = dist.get("ok", 0)
    print(f"可直接索引: {ok}/{len(reports)}"
          + (f"，其余 {len(reports)-ok} 个需要先规范化或改走分块+摘要"
             if ok < len(reports) else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
