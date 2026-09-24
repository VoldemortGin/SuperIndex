#!/usr/bin/env python3
"""
Stage 1 - Build the PageIndex tree index over the AIA annual reports.

This stage is fully offline: PageIndex "flash" mode derives the tree from PDF
layout statistics without any LLM. We run it with summary=False and
optimize=False so no API key is required.

Usage:
    uv run python scripts/01_build_index.py [--with-llm]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "aia_reports"
OUT_DIR = ROOT / "results"


def count_nodes(structure: list) -> tuple[int, int]:
    """Return (total_nodes, max_depth)."""
    total = 0
    depth = 0

    def walk(nodes: list, level: int) -> None:
        nonlocal total, depth
        for n in nodes:
            total += 1
            depth = max(depth, level)
            walk(n.get("nodes") or [], level + 1)

    walk(structure, 1)
    return total, depth


def page_count(pdf: Path) -> int:
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(pdf))
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return -1


def build_one(pdf: Path, with_llm: bool) -> dict:
    from superindex.engine.flash import page_index_flash

    t0 = time.time()
    tree = page_index_flash(
        str(pdf),
        summary=with_llm,
        optimize="full" if with_llm else False,
    )
    elapsed = time.time() - t0

    structure = tree.get("structure") or []
    total, depth = count_nodes(structure)

    out = OUT_DIR / f"{pdf.stem}_structure.json"
    out.write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "file": pdf.name,
        "pages": page_count(pdf),
        "size_mb": round(pdf.stat().st_size / 1024 / 1024, 2),
        "toc_source": tree.get("toc_source"),
        "top_level_sections": len(structure),
        "total_nodes": total,
        "max_depth": depth,
        "seconds": round(elapsed, 2),
        "tree_json": out.name,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--with-llm",
        action="store_true",
        help="also generate node summaries + tree optimization (needs an LLM key)",
    )
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(DATA_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {DATA_DIR}", file=sys.stderr)
        return 1

    print(f"Building PageIndex trees for {len(pdfs)} document(s)")
    print(f"  LLM summaries/optimize: {'ON' if args.with_llm else 'OFF (offline)'}\n")

    rows = []
    for pdf in pdfs:
        print(f"  -> {pdf.name} ...", end="", flush=True)
        try:
            row = build_one(pdf, args.with_llm)
        except Exception as exc:  # noqa: BLE001
            print(f" FAILED: {type(exc).__name__}: {exc}")
            continue
        rows.append(row)
        print(
            f" {row['pages']}p, {row['total_nodes']} nodes, "
            f"depth {row['max_depth']}, toc={row['toc_source']}, "
            f"{row['seconds']}s"
        )

    summary_path = OUT_DIR / "index_summary.json"
    summary_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nWrote {len(rows)} tree(s) to {OUT_DIR}/")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
