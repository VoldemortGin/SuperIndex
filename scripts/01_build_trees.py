#!/usr/bin/env python3
"""
Stage 1 — Build PageIndex tree structures for the AIA annual reports.

This stage is fully offline: `page_index_flash(..., summary=False, optimize=False)`
derives the hierarchy from the PDF layout statistics only, so no LLM API key
is required. It gives us the document skeleton (and a quick sanity check that
PageIndex can parse these PDFs) before any paid LLM work happens.

Usage:
    python scripts/01_build_trees.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pypdfium2 as pdfium
from pageindex.flash import page_index_flash

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "aia_reports"
OUT_DIR = ROOT / "results" / "trees"


def walk(nodes, depth=1):
    """Yield (node, depth) for every node in the tree."""
    for node in nodes or []:
        yield node, depth
        yield from walk(node.get("nodes"), depth + 1)


def summarise(tree: dict) -> dict:
    flat = list(walk(tree.get("structure")))
    pages = 0
    for node, _ in flat:
        start, end = node.get("start_index"), node.get("end_index")
        if isinstance(start, int) and isinstance(end, int):
            pages += max(0, end - start + 1)
    return {
        "nodes": len(flat),
        "max_depth": max((d for _, d in flat), default=0),
        "leaf_pages_covered": pages,
        "toc_source": tree.get("toc_source"),
    }


def main() -> int:
    pdfs = sorted(DATA_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {DATA_DIR}", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []

    print(f"{'document':<34} {'pages':>6} {'nodes':>7} {'depth':>6} {'src':>10} {'secs':>7}")
    print("-" * 78)

    for pdf in pdfs:
        doc = pdfium.PdfDocument(str(pdf))
        page_count = len(doc)
        doc.close()

        t0 = time.time()
        tree = page_index_flash(str(pdf), summary=False, optimize=False)
        elapsed = time.time() - t0

        stats = summarise(tree)
        out_file = OUT_DIR / f"{pdf.stem}_structure.json"
        out_file.write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")

        print(f"{pdf.stem:<34} {page_count:>6} {stats['nodes']:>7} "
              f"{stats['max_depth']:>6} {str(stats['toc_source']):>10} {elapsed:>7.1f}")

        manifest.append({
            "document": pdf.name,
            "path": str(pdf.relative_to(ROOT)),
            "pages": page_count,
            "tree_file": str(out_file.relative_to(ROOT)),
            **stats,
            "index_seconds": round(elapsed, 2),
            "indexed_with_llm": False,
        })

    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print("-" * 78)
    print(f"Wrote {len(manifest)} tree files to {OUT_DIR.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
